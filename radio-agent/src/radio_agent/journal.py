from datetime import datetime, timezone
from pathlib import Path
import json
import os
import time
import uuid
import wave


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


class Journal:
    def __init__(self, settings, mode):
        self.settings = settings
        self.run_id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.path = Path(settings.log_dir) / self.run_id
        self.path.mkdir(parents=True, mode=0o700)
        self.handle = (self.path / 'events.jsonl').open('a', encoding='utf-8', buffering=1)
        self.started = time.monotonic()
        self.turns = 0
        self.emit('run_started', mode=mode, settings=settings.public())

    def emit(self, event, turn_id=None, **fields):
        value = dict(timestamp=now(), elapsed=round(time.monotonic() - self.started, 3),
                     run_id=self.run_id, event=event, **fields)
        if turn_id:
            value['turn_id'] = turn_id
        # Redact even server error bodies before writing them.
        line = self.settings.redact(json.dumps(value, ensure_ascii=False))
        self.handle.write(line + '\n')
        if event not in ('cloud_event', 'audio_packet'):
            print(line, flush=True)

    def new_turn(self, conversation_id):
        self.turns += 1
        return TurnLog(self, f'{self.turns:04d}-{uuid.uuid4().hex[:8]}', conversation_id)

    def close(self):
        self.handle.flush()
        self.handle.close()


class TurnLog:
    def __init__(self, journal, turn_id, conversation_id):
        self.journal, self.turn_id = journal, turn_id
        self.path = journal.path / turn_id
        self.path.mkdir(mode=0o700)
        self.data = dict(turn_id=turn_id, conversation_id=conversation_id, started_at=now(),
                         status='receiving', user_text='', user_text_status='missing',
                         assistant_text='', assistant_text_status='missing', spoken_text='',
                         transmission='not_started', timings={}, files={})
        self.streams = {}
        self.started = time.monotonic()
        for name, rate in (('rx', 48000), ('uplink', 16000), ('reply', 24000)):
            path = self.path / f'{name}.wav'
            wav = wave.open(str(path), 'wb')
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            self.streams[name] = wav
            self.data['files'][name] = path.name
        self.save()
        self.event('turn_started')

    def event(self, event, **fields):
        self.data['timings'].setdefault(event, round(time.monotonic() - self.started, 3))
        self.journal.emit(event, self.turn_id, **fields)

    def audio(self, name, data):
        if len(data) % 2:
            raise ValueError('PCM16 audio must contain whole samples')
        self.streams[name].writeframes(data)

    def save(self):
        value = json.loads(self.journal.settings.redact(json.dumps(self.data, ensure_ascii=False)))
        atomic_json(self.path / 'turn.json', value)

    def finish(self, status, error=None):
        self.data.update(status=status, ended_at=now())
        if error:
            self.data['error'] = self.journal.settings.redact(error)
        for wav in self.streams.values():
            wav.close()
        self.streams.clear()
        self.save()
        self.event('turn_finished', status=status, transmission=self.data['transmission'])
        # Human-readable view, in addition to structured metadata and raw events.
        text = (f"开始: {self.data['started_at']}\n状态: {status}\n"
                f"用户 [{self.data['user_text_status']}]: {self.data['user_text']}\n"
                f"AI [{self.data['assistant_text_status']}]: {self.data['assistant_text']}\n"
                f"合成语音文本: {self.data['spoken_text']}\n"
                f"发射: {self.data['transmission']}\n")
        (self.path / 'transcript.txt').write_text(self.journal.settings.redact(text), encoding='utf-8')

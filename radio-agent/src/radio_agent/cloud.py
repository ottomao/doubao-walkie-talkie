import asyncio
import contextlib
import time
import uuid

from websockets.asyncio.client import connect

from .protocol import encode, decode


class CloudError(RuntimeError):
    pass


class Reply:
    def __init__(self, turn):
        self.turn = turn
        self.audio = bytearray()
        self.sentence_ends = []
        self.user_final = False
        self.asr_ended = self.chat_ended = self.tts_ended = False
        self.question_id = self.reply_id = None

    @property
    def complete(self):
        return self.asr_ended and self.chat_ended and self.tts_ended

    def accept(self, event):
        body = event.payload
        if isinstance(body, dict):
            question = body.get('question_id')
            reply = body.get('reply_id')
            if question:
                if self.question_id and question != self.question_id:
                    raise CloudError('Unexpected question ID; refusing to mix different turns')
                self.question_id = question
            if reply:
                if self.reply_id and reply != self.reply_id:
                    raise CloudError('Unexpected reply ID; refusing to mix different responses')
                self.reply_id = reply
            self.turn.event('cloud_event', event_id=event.event, payload=body)
        else:
            self.turn.event('cloud_event', event_id=event.event, audio_bytes=len(body))
        if event.event == 451:
            results = body.get('results', [])
            # ASR results are revisable snapshots, not text deltas.
            text = ''.join(r.get('text', '') for r in results)
            if text:
                self.turn.data['user_text'] = text
                self.user_final = all(r.get('is_interim') is False for r in results)
                self.turn.data['user_text_status'] = 'final' if self.user_final else 'partial'
                self.turn.save()
        elif event.event == 459:
            self.asr_ended = True
            self.turn.event('asr_ended')
        elif event.event == 550:
            self.turn.data['assistant_text'] += body.get('content', '')
            self.turn.data['assistant_text_status'] = 'partial'
            self.turn.save()
        elif event.event == 559:
            self.chat_ended = True
            if self.turn.data['assistant_text']:
                self.turn.data['assistant_text_status'] = 'final'
            self.turn.event('text_complete')
        elif event.event == 350:
            self.turn.data['spoken_text'] += body.get('text', '')
        elif event.event == 352:
            if not isinstance(body, bytes):
                raise CloudError('Expected PCM bytes for TTSResponse')
            if len(self.audio) + len(body) > 24000 * 2 * 60:
                raise CloudError('Reply exceeded 60-second buffer limit')
            if not self.audio:
                self.turn.event('first_reply_audio')
            self.audio.extend(body)
            self.turn.audio('reply', body)
        elif event.event == 351:
            self.sentence_ends.append(len(self.audio))
        elif event.event == 359:
            self.tts_ended = True
            self.turn.event('reply_audio_complete')
        self.turn.data.update(question_id=self.question_id, reply_id=self.reply_id)


class RealtimeClient:
    def __init__(self, settings, journal):
        self.settings, self.journal = settings, journal
        self.ws = None
        self.session_id = ''
        self.dialog_id = ''
        self.collector = None
        self.reply = None
        self.next_send = 0

    async def _receive(self):
        event = decode(await self.ws.recv())
        if event.error_code is not None or event.event in (51, 153, 599):
            raise CloudError(f'Cloud error {event.error_code or event.event}: {event.payload}')
        if event.event >= 100 and event.session_id != self.session_id:
            raise CloudError('Received event for a different session')
        return event

    async def _expect(self, expected):
        while True:
            event = await self._receive()
            self.journal.emit('cloud_event', event_id=event.event, payload=event.payload)
            if event.event == expected:
                return event
            if event.event in (52, 152):
                raise CloudError('Cloud session ended unexpectedly')

    async def open(self):
        self.settings.require_credentials()
        connection_id = str(uuid.uuid4())
        headers = {'X-Api-Resource-Id': 'volc.speech.dialog',
                   'X-Api-Connect-Id': connection_id,
                   'X-Api-Request-Id': connection_id}
        if self.settings.api_key:
            headers['X-Api-Key'] = self.settings.api_key
        else:
            headers.update({'X-Api-App-ID': self.settings.app_id,
                            'X-Api-Access-Key': self.settings.access_key,
                            'X-Api-App-Key': 'PlgvMymc7f3tQnJ6'})
        try:
            self.ws = await connect(self.settings.endpoint, additional_headers=headers,
                                    open_timeout=10, close_timeout=2, ping_interval=20,
                                    max_size=8 * 1024 * 1024, max_queue=64, proxy=None)
            self.journal.emit('cloud_connected', connection_id=connection_id,
                              log_id=self.ws.response.headers.get('X-Tt-Logid'))
            await self.ws.send(encode(1))
            await asyncio.wait_for(self._expect(50), 10)
            self.session_id = str(uuid.uuid4())
            payload = {
                'asr': {'extra': {'enable_asr_twopass': True}},
                'tts': {'speaker': self.settings.speaker, 'extra': {},
                        'audio_config': {'channel': 1, 'format': 'pcm_s16le', 'sample_rate': 24000}},
                'dialog': {'bot_name': '小台',
                           'system_role': '你是通过对讲机交流的中文语音助手。先直接回答问题，通常只说一到三句，尽量在十五秒内说完。不要使用Markdown、表格或长列表。听不清时简短请对方重说。',
                           'speaking_style': '使用清楚自然的普通话，语气友好，表达简洁。',
                           'extra': {'model': self.settings.model, 'input_mod': 'push_to_talk',
                                     'enable_loudness_norm': True, 'strict_audit': True}}}
            if self.dialog_id:
                payload['dialog']['dialog_id'] = self.dialog_id
            await self.ws.send(encode(100, payload, self.session_id))
            result = await asyncio.wait_for(self._expect(150), 10)
            self.dialog_id = result.payload.get('dialog_id', '')
            self.journal.emit('cloud_session_started', session_id=self.session_id, dialog_id=self.dialog_id)
        except BaseException:
            if self.ws:
                await self.ws.close()
            self.ws = None
            raise

    async def begin(self, turn):
        if self.collector is not None:
            raise RuntimeError('Previous turn is still active')
        if self.ws is None:
            await self.open()
        self.reply = Reply(turn)
        turn.data.update(session_id=self.session_id, dialog_id=self.dialog_id)
        turn.save()
        self.next_send = time.monotonic()

        async def collect():
            while not self.reply.complete:
                event = await self._receive()
                if event.event in (52, 152):
                    raise CloudError('Cloud session ended during a turn')
                self.reply.accept(event)
            return self.reply

        self.collector = asyncio.create_task(collect())

    async def send_audio(self, packet):
        if self.collector.done():
            self.collector.result()  # propagate early cloud errors
            raise CloudError('Cloud responded before client finished input')
        # Pace both pre-roll and live audio at 20 ms; no burst uploads.
        delay = self.next_send - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        await asyncio.wait_for(self.ws.send(encode(200, packet, self.session_id)), 5)
        self.next_send = max(self.next_send + 0.02, time.monotonic())

    async def finish_input(self):
        await self.ws.send(encode(400, {}, self.session_id))
        self.reply.turn.event('input_committed')
        result = await asyncio.wait_for(self.collector, self.settings.reply_timeout_seconds)
        self.collector = None
        if not result.audio:
            raise CloudError('Cloud returned no reply audio')
        if not result.user_final:
            result.turn.event('transcript_incomplete', role='user')
        if result.turn.data['assistant_text_status'] != 'final':
            result.turn.event('transcript_incomplete', role='assistant')
        return result

    async def close(self, reset=False):
        if self.collector:
            self.collector.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.collector
            self.collector = None
        if self.ws:
            try:
                if self.session_id:
                    await self.ws.send(encode(102, {}, self.session_id))
                    await asyncio.wait_for(self._expect(152), 2)
                await self.ws.send(encode(2))
            except Exception:
                pass
            finally:
                await self.ws.close()
                self.ws = None
        self.session_id = ''
        if reset:
            self.dialog_id = ''

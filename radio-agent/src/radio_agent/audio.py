import asyncio
from collections import deque
import math
import wave

import numpy as np
import soxr
import sounddevice as sd
import webrtcvad
from aioc.device import audio_device, ConnectionGuard

RATE = 48000
FRAME_SAMPLES = 960  # 20 ms


def pcm16(samples):
    return np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()


def resample(data, source_rate, target_rate):
    samples = np.frombuffer(data, dtype='<i2').astype(np.float32)
    return pcm16(soxr.resample(samples, source_rate, target_rate))


class UplinkResampler:
    def __init__(self):
        self.stream = soxr.ResampleStream(48000, 16000, 1, dtype='float32', quality='HQ')
        self.pending = bytearray()

    def feed(self, data, final=False):
        samples = np.frombuffer(data, dtype='<i2').astype(np.float32)
        self.pending.extend(pcm16(self.stream.resample_chunk(samples, last=final)))
        packets = []
        while len(self.pending) >= 640:
            packets.append(bytes(self.pending[:640]))
            del self.pending[:640]
        if final and self.pending:
            packets.append(bytes(self.pending).ljust(640, b'\0'))
            self.pending.clear()
        return packets


def write_wav(path, data, rate):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data)


def read_wav(path):
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE':
            raise ValueError('Replay requires a mono 16-bit PCM WAV')
        if wav.getnframes() > wav.getframerate() * 60:
            raise ValueError('Replay WAV must be at most 60 seconds')
        return wav.readframes(wav.getnframes()), wav.getframerate()


class Segmenter:
    """WebRTC speech classification plus energy gate, onset debounce and pre-roll."""
    def __init__(self, settings):
        self.settings = settings
        self.vad = webrtcvad.Vad(2)
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=15)  # 300 ms
        self.votes = deque(maxlen=8)
        self.active = False
        self.silence = self.frames = 0

    def is_speech(self, frame):
        if len(frame) != FRAME_SAMPLES * 2:
            raise ValueError('Expected a 20 ms 48 kHz PCM16 frame')
        samples = np.frombuffer(frame, dtype='<i2').astype(np.float64)
        dbfs = 20 * math.log10(max(float(np.sqrt(np.mean(samples ** 2))) / 32768, 1e-12))
        return dbfs >= self.settings.vad_min_dbfs and self.vad.is_speech(frame, RATE)

    def feed(self, frame, speech=None):
        if len(frame) != FRAME_SAMPLES * 2:
            raise ValueError('Expected a 20 ms 48 kHz PCM16 frame')
        if speech is None:
            speech = self.is_speech(frame)
        if not self.active:
            self.pre.append(frame)
            self.votes.append(bool(speech))
            if len(self.votes) == 8 and sum(self.votes) >= 5:
                self.active = True
                self.frames = len(self.pre)
                return 'start', list(self.pre)
            return None, []
        self.frames += 1
        self.silence = 0 if speech else self.silence + 1
        reason = None
        if self.frames * 0.02 >= self.settings.max_input_seconds:
            reason = 'limit'
        elif self.silence * 0.02 >= self.settings.silence_seconds:
            reason = 'end'
        if reason:
            self.reset()
            return reason, [frame]
        return 'chunk', [frame]


class Capture:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=250)
        self.accepting = True
        self.error = None
        self.generation = 0
        self.stream = None

    def set_accepting(self, value):
        self.accepting = value
        self.generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        self.error = None

    def open(self):
        self.guard = ConnectionGuard()
        loop = asyncio.get_running_loop()

        def deliver(data, status, generation):
            if not self.accepting or generation != self.generation:
                return
            if status:
                self.error = RuntimeError(f'AIOC capture status: {status}')
            if self.queue.full():
                self.error = RuntimeError('Capture queue overflow; turn discarded rather than dropping audio')
            else:
                self.queue.put_nowait(data)

        def callback(indata, frames, timing, status):
            if self.accepting:
                loop.call_soon_threadsafe(deliver, bytes(indata), str(status) if status else '', self.generation)

        self.stream = sd.RawInputStream(device=audio_device('input'), channels=1, samplerate=RATE,
                                        dtype='int16', blocksize=FRAME_SAMPLES, callback=callback)
        self.stream.start()
        return self

    async def read(self):
        self.guard.check()
        if self.error:
            raise self.error
        data = await asyncio.wait_for(self.queue.get(), 2)
        self.guard.check()
        if self.error:
            raise self.error
        return data

    def close(self):
        self.set_accepting(False)
        if self.stream:
            stream, self.stream = self.stream, None
            try:
                stream.abort()
            except sd.PortAudioError:
                pass  # Device removal can already have stopped the stream.
            finally:
                stream.close()

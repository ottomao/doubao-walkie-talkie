"""USB serial PTT and explicitly selected AIOC audio devices."""
import contextlib
import math
import os
import time
import wave

import numpy as np
import serial
from serial.tools import list_ports
import sounddevice as sd


class DeviceDisconnected(RuntimeError):
    pass


class ConnectionGuard:
    """A replug is a new connection even when macOS reuses the serial path."""
    def __init__(self):
        self.port = serial_device()
        self.identity = self.snapshot()

    def snapshot(self):
        try:
            info = os.stat(self.port)
            return info.st_ino, info.st_ctime_ns
        except OSError as exc:
            raise DeviceDisconnected('AIOC disconnected; reconnect and restart radio-agent') from exc

    def check(self):
        if self.snapshot() != self.identity:
            raise DeviceDisconnected('AIOC was replugged; restart radio-agent to reopen audio devices')


def inventory():
    ports = [dict(port=p.device, serial=p.serial_number, description=p.description)
             for p in list_ports.comports() if (p.vid, p.pid) == (0x1209, 0x7388)]
    audio = [dict(index=i, name=d['name'], inputs=d['max_input_channels'],
                  outputs=d['max_output_channels'], sample_rate=d['default_samplerate'])
             for i, d in enumerate(sd.query_devices()) if 'aioc' in d['name'].lower()]
    return dict(serial_devices=ports, audio_devices=audio)


def audio_device(direction, index=None):
    key = 'max_input_channels' if direction == 'input' else 'max_output_channels'
    devices = sd.query_devices()
    candidates = [i for i, d in enumerate(devices)
                  if 'aioc' in d['name'].lower() and d[key] > 0]
    if index is not None:
        if index not in candidates:
            raise ValueError(f'Audio device {index} is not an AIOC {direction} device')
        return index
    if len(candidates) != 1:
        raise ValueError(f'Expected one AIOC {direction} device; found {candidates}. Select its index explicitly.')
    return candidates[0]


def serial_device(port=None):
    candidates = [p['port'] for p in inventory()['serial_devices']]
    if port is not None and port in candidates:
        return port
    if port is not None or len(candidates) != 1:
        raise ValueError(f'Select an AIOC serial port from {candidates}')
    return candidates[0]


@contextlib.contextmanager
def control(port=None):
    # Configure inactive lines BEFORE open; pyserial otherwise defaults DTR on.
    device = serial.Serial(port=None, baudrate=9600, timeout=0.5, exclusive=True)
    device.dtr = False
    device.rts = False
    device.port = serial_device(port)
    try:
        device.open()
        yield device
    finally:
        if device.is_open:
            try:
                device.dtr = False
            finally:
                try:
                    device.rts = False
                finally:
                    device.close()


def duration(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError('Duration must be greater than 0 and at most 60 seconds')
    return value


def check_playback(seconds=2):
    """Measure USB playback timing using silence, without asserting PTT."""
    seconds = duration(seconds)
    index = audio_device('output')
    data = np.zeros((round(seconds * 48000), 1), dtype='int16')
    with sd.OutputStream(device=index, channels=1, samplerate=48000, dtype='int16') as stream:
        started = time.monotonic()
        underflow = stream.write(data)
        stream.stop()
        elapsed = time.monotonic() - started
        latency = stream.latency
    return dict(expected_seconds=seconds, elapsed_seconds=round(elapsed, 3),
                latency_seconds=latency, underflow=bool(underflow),
                timing_suspect=elapsed > seconds + max(0.5, 2 * latency))


def ptt(seconds, port=None):
    seconds = duration(seconds)
    with control(port) as device:
        device.dtr = True
        time.sleep(seconds)
    return dict(ptt='released', seconds=seconds)


def record(path, seconds, index=None):
    seconds = duration(seconds)
    index = audio_device('input', index)
    rate = 48000
    chunks = []
    overflows = 0
    with sd.InputStream(device=index, channels=1, samplerate=rate, dtype='int16') as stream:
        remaining = round(seconds * rate)
        while remaining:
            count = min(4800, remaining)
            data, overflow = stream.read(count)
            chunks.append(data.copy())
            overflows += int(overflow)
            remaining -= count
    data = np.concatenate(chunks)
    # Exclusive creation prevents accidental overwrite of previous recordings.
    with open(path, 'xb') as output:
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(data.astype('<i2').tobytes())
    normalized = data.astype(np.float64) / 32768
    rms = float(np.sqrt(np.mean(normalized ** 2)))
    return dict(path=str(path), seconds=seconds, frames=len(data), sample_rate=rate,
                peak=float(np.max(np.abs(normalized))),
                rms_dbfs=20 * math.log10(rms) if rms else None, overflows=overflows)


def transmit(path, port=None, index=None, gain=0.25):
    if not math.isfinite(gain) or not 0 < gain <= 1:
        raise ValueError('Gain must be greater than 0 and at most 1')
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 48000, 'NONE'):
            raise ValueError('Use a mono, 16-bit PCM, 48000 Hz WAV file')
        seconds = duration(wav.getnframes() / 48000)
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768 * gain
    index = audio_device('output', index)
    guard = ConnectionGuard()
    underflows = 0
    # Open audio before keying; always release PTT on failures and interruption.
    with sd.OutputStream(device=index, channels=1, samplerate=48000, dtype='float32') as stream:
        stream.stop()
        with control(port) as device:
            guard.check()
            device.dtr = True
            try:
                time.sleep(0.15)
                stream.start()
                for start in range(0, len(data), 4800):
                    guard.check()
                    underflows += int(stream.write(data[start:start + 4800]))
                stream.stop()  # Drain queued samples before releasing PTT.
                time.sleep(0.1)
            finally:
                try:
                    device.dtr = False
                finally:
                    stream.abort()
    return dict(ptt='released', seconds=seconds, underflows=underflows)


def listen(seconds, index=None):
    seconds = duration(seconds)
    index = audio_device('input', index)
    output = sd.default.device[1]
    if output < 0 or 'aioc' in sd.query_devices(output)['name'].lower():
        raise ValueError('Choose Mac speakers/headphones as the system default output before listening')
    overflows = underflows = 0
    with sd.InputStream(device=index, channels=1, samplerate=48000, dtype='float32') as source:
        with sd.OutputStream(device=output, channels=1, samplerate=48000, dtype='float32') as sink:
            remaining = round(seconds * 48000)
            while remaining:
                count = min(960, remaining)
                data, overflow = source.read(count)
                overflows += int(overflow)
                underflows += int(sink.write(data))
                remaining -= count
    return dict(seconds=seconds, overflows=overflows, underflows=underflows)

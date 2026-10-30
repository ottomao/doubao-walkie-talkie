import asyncio
import contextlib
import json
import sys
import time
import uuid

import numpy as np
from aioc import device
from aioc.config import inspect

from .audio import Capture, Segmenter, UplinkResampler, read_wav, resample, write_wav
from .cloud import RealtimeClient


class InputTooLong(RuntimeError):
    pass


def verify_radio():
    inventory = device.inventory()
    if len(inventory['serial_devices']) != 1:
        raise ValueError('This version requires exactly one connected AIOC')
    device.audio_device('input')
    device.audio_device('output')
    configuration = inspect(inventory['serial_devices'][0]['serial'])
    if configuration['auto_ptt'] or not configuration['serial_ptt']:
        raise ValueError('AIOC needs serial PTT with AutoPTT disabled. Run aioc config --serial-ptt first.')
    return dict(devices=inventory, configuration=configuration)


def prepare_transmission(reply, settings):
    """Keep a complete sentence if possible; record a hard cut explicitly otherwise."""
    limit = int(settings.max_reply_seconds * 24000) * 2
    cut = len(reply.audio)
    truncated = cut > limit
    if truncated:
        endings = [end for end in reply.sentence_ends if 0 < end <= limit]
        cut = max(endings) if endings else limit
    data = resample(bytes(reply.audio[:cut]), 24000, 48000)
    samples = np.frombuffer(data, dtype='<i2').astype(np.float64) * settings.tx_gain
    # Short ramps suppress keying clicks and make any hard cutoff less abrupt.
    ramp = min(480, len(samples) // 2)
    if ramp:
        samples[:ramp] *= np.linspace(0, 1, ramp)
        samples[-ramp:] *= np.linspace(1, 0, ramp)
    data = np.clip(np.rint(samples), -32768, 32767).astype('<i2').tobytes()
    return data, truncated


async def transmit_file(path, seconds, turn):
    """Isolated CLI process: cancellation never leaves a background thread keying PTT."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'aioc.cli', '--json', 'transmit', str(path), '--gain', '1',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        turn.data['transmission'] = 'in_progress'
        turn.save()
        turn.event('transmit_started', pid=process.pid, seconds=seconds)
        stdout, stderr = await asyncio.wait_for(process.communicate(), seconds + 5)
        if process.returncode:
            raise RuntimeError(f'Transmit failed: {stderr.decode(errors="replace").strip()}')
        report = json.loads(stdout)
        if not report.get('ok') or report['result'].get('ptt') != 'released':
            raise RuntimeError('Transmit did not confirm PTT release')
        turn.event('transmit_finished', result=report['result'])
        return report['result']
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
        if process.returncode != 0:
            # Also retry release after ordinary failures and cancellation.
            release = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'aioc.cli', '--json', 'release',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, error = await asyncio.wait_for(release.communicate(), 3)
                if release.returncode:
                    turn.event('ptt_release_failed', error=error.decode(errors='replace'))
            except asyncio.TimeoutError:
                release.kill()
                await release.communicate()
                turn.event('ptt_release_failed', error='Release timed out; disconnect AIOC if transmitting')


class Engine:
    def __init__(self, settings, journal, client=None):
        self.settings, self.journal = settings, journal
        self.client = client or RealtimeClient(settings, journal)
        self.conversation_id = uuid.uuid4().hex
        self.reset_requested = False
        self.last_activity = time.monotonic()

    async def reset(self, reason):
        await self.client.close(reset=True)
        self.conversation_id = uuid.uuid4().hex
        self.reset_requested = False
        self.last_activity = time.monotonic()
        self.journal.emit('context_reset', reason=reason, conversation_id=self.conversation_id)

    async def connect(self):
        if self.client.ws is not None:
            return
        for attempt in range(5):
            try:
                await self.client.open()
                return
            except Exception as exc:
                self.journal.emit('connect_failed', attempt=attempt + 1, error=self.settings.redact(exc))
                response = getattr(exc, 'response', None)
                if getattr(response, 'status_code', None) in (400, 401, 403) or attempt == 4:
                    raise
                await asyncio.sleep(min(2 ** attempt, 10))

    async def upload(self, turn, converter, frames, final=False, record_rx=True):
        for frame in frames:
            if record_rx:
                turn.audio('rx', frame)
            for packet in converter.feed(frame):
                turn.audio('uplink', packet)
                await self.client.send_audio(packet)
        if final:
            for packet in converter.feed(b'', final=True):
                turn.audio('uplink', packet)
                await self.client.send_audio(packet)

    async def respond(self, turn, transmit):
        turn.data['status'] = 'waiting_reply'
        turn.save()
        turn.event('waiting_reply')
        reply = await self.client.finish_input()
        data, truncated = prepare_transmission(reply, self.settings)
        path = turn.path / 'tx.wav'
        write_wav(path, data, 48000)
        turn.data['files']['tx'] = path.name
        turn.data.update(reply_truncated=truncated, tx_gain=self.settings.tx_gain,
                         tx_seconds=len(data) / 96000, generated_seconds=len(reply.audio) / 48000)
        turn.save()
        if transmit:
            if hasattr(self, 'guard'):
                self.guard.check()
            result = await transmit_file(path, len(data) / 96000, turn)
            turn.data['transmission'] = 'completed_with_underflow' if result['underflows'] else 'completed'
            if truncated:
                turn.data['transmission'] += '_truncated'
        else:
            turn.data['transmission'] = 'not_requested'
        turn.finish('completed')
        if truncated or not transmit:
            # Do not let subsequent replies presume the listener heard unplayed text.
            await self.reset('reply_truncated' if truncated else 'reply_not_transmitted')
        self.last_activity = time.monotonic()

    async def replay(self, path, transmit=False):
        pcm, rate = read_wav(path)
        if not pcm:
            raise ValueError('Empty WAV')
        if transmit:
            verify_radio()
        if rate != 48000:
            pcm = resample(pcm, rate, 48000)
        if len(pcm) / 96000 > self.settings.max_input_seconds:
            raise ValueError('Replay exceeds configured max input duration')
        turn = self.journal.new_turn(self.conversation_id)
        converter = UplinkResampler()
        try:
            turn.audio('rx', pcm)
            await self.connect()
            await self.client.begin(turn)
            await self.upload(turn, converter, [pcm], final=True, record_rx=False)
            turn.event('input_ended', reason='file_complete')
            await self.respond(turn, transmit)
        except BaseException as exc:
            if turn.streams:
                if turn.data['transmission'] == 'in_progress':
                    turn.data['transmission'] = 'failed_or_interrupted'
                turn.finish('interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed', str(exc))
            raise
        finally:
            await self.client.close()

    async def run(self, once=False):
        verify_radio()
        capture = Capture()
        segmenter = Segmenter(self.settings)
        turn = None
        try:
            await self.connect()
            capture.open()
            self.guard = capture.guard
            self.journal.emit('listening')
            while True:
                if not turn and (self.reset_requested or time.monotonic() - self.last_activity > self.settings.idle_reset_seconds):
                    await self.reset('requested' if self.reset_requested else 'idle_timeout')
                try:
                    frame = await capture.read()
                    kind, frames = segmenter.feed(frame)
                    if kind == 'start':
                        turn = self.journal.new_turn(self.conversation_id)
                        converter = UplinkResampler()
                        for chunk in frames:
                            turn.audio('rx', chunk)
                        await self.connect()
                        await self.client.begin(turn)
                    if turn and frames:
                        await self.upload(turn, converter, frames, final=kind == 'end', record_rx=kind != 'start')
                    if kind == 'limit':
                        raise InputTooLong('Input exceeded duration limit; waiting for quiet before listening again')
                    if kind == 'end':
                        turn.event('input_ended', reason='silence')
                        capture.set_accepting(False)
                        self.journal.emit('input_paused', reason='waiting_and_transmitting')
                        await self.respond(turn, transmit=True)
                        turn = None
                        if once:
                            return
                        await asyncio.sleep(self.settings.guard_seconds)
                        capture.set_accepting(True)
                        self.journal.emit('listening')
                except Exception as exc:
                    capture.set_accepting(False)
                    if turn:
                        if turn.data['transmission'] == 'in_progress':
                            turn.data['transmission'] = 'failed_or_interrupted'
                        turn.finish('failed', str(exc))
                        turn = None
                    self.journal.emit('recovering', error=self.settings.redact(exc))
                    await self.reset('turn_or_device_error')
                    if once or isinstance(exc, (device.DeviceDisconnected, OSError)) or 'Transmit failed' in str(exc):
                        raise
                    segmenter.reset()
                    capture.close()
                    await asyncio.sleep(1)
                    self.guard.check()
                    verify_radio()
                    await self.connect()
                    capture = Capture().open()
                    if isinstance(exc, InputTooLong):
                        self.journal.emit('waiting_for_channel_silence')
                        quiet = 0
                        while quiet * 0.02 < self.settings.silence_seconds:
                            quiet = 0 if segmenter.is_speech(await capture.read()) else quiet + 1
                    self.journal.emit('listening')
        finally:
            capture.close()
            if turn and turn.streams:
                if turn.data['transmission'] == 'in_progress':
                    turn.data['transmission'] = 'failed_or_interrupted'
                turn.finish('interrupted')
            await self.client.close()


async def listen_only(settings, journal, seconds):
    """Local receive/VAD smoke test; neither cloud upload nor transmission."""
    capture = Capture().open()
    segmenter = Segmenter(settings)
    turn = None
    deadline = time.monotonic() + seconds
    try:
        journal.emit('listening', cloud=False, transmit=False)
        while time.monotonic() < deadline:
            frame = await capture.read()
            kind, frames = segmenter.feed(frame)
            if kind == 'start':
                turn = journal.new_turn('listen-only')
            if turn:
                for chunk in frames:
                    turn.audio('rx', chunk)
                if kind in ('end', 'limit'):
                    turn.finish('captured_only')
                    turn = None
    finally:
        capture.close()
        if turn:
            turn.finish('captured_partial')

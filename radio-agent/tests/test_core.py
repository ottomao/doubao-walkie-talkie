import asyncio
import contextlib
import gzip
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from websockets.asyncio.server import serve

from radio_agent.protocol import encode, decode
from radio_agent.settings import Settings
from radio_agent.audio import Segmenter, UplinkResampler, read_wav, write_wav
from radio_agent.journal import Journal
from radio_agent.cloud import Reply, RealtimeClient, CloudError
from radio_agent.engine import Engine, prepare_transmission


def server_frame(event, payload=None, sid='session', compress=False):
    binary = isinstance(payload, bytes)
    data = payload if binary else json.dumps(payload or {}).encode()
    if compress:
        data = gzip.compress(data)
    frame = bytes((0x11, 0xb4 if binary else 0x94, (0 if binary else 0x10) | int(compress), 0))
    key = sid.encode()
    return frame + struct.pack('>II', event, len(key)) + key + struct.pack('>I', len(data)) + data


def client_frame(frame):
    event = struct.unpack('>I', frame[4:8])[0]
    pos = 8
    sid = ''
    if event >= 100:
        n = struct.unpack('>I', frame[pos:pos + 4])[0]
        sid = frame[pos + 4:pos + 4 + n].decode()
        pos += 4 + n
    n = struct.unpack('>I', frame[pos:pos + 4])[0]
    data = frame[pos + 4:]
    assert len(data) == n
    return event, sid, data


class ProtocolTests(unittest.TestCase):
    def test_official_start_connection_example(self):
        self.assertEqual(list(encode(1)), [17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125])

    def test_audio_session_and_gzip(self):
        pcm = b'\x01\x00' * 320
        self.assertEqual(client_frame(encode(200, pcm, 'session')), (200, 'session', pcm))
        event = decode(server_frame(451, {'results': []}, compress=True))
        self.assertEqual(event.payload, {'results': []})
        self.assertEqual(decode(server_frame(352, pcm)).payload, pcm)

    def test_truncated_frames_rejected(self):
        message = server_frame(450, {'question_id': 'q'})
        for length in range(len(message)):
            with self.assertRaises((ValueError, UnicodeDecodeError)):
                decode(message[:length])

    def test_error_frame(self):
        data = b'{"error":"denied"}'
        message = bytes((0x11, 0xf0, 0x10, 0)) + struct.pack('>II', 45000001, len(data)) + data
        self.assertEqual(decode(message).error_code, 45000001)


class AudioTests(unittest.TestCase):
    def test_onset_preroll_and_one_second_end(self):
        segmenter = Segmenter(Settings())
        frame = bytes(1920)
        for _ in range(15):
            self.assertEqual(segmenter.feed(frame, speech=False)[0], None)
        for _ in range(4):
            self.assertEqual(segmenter.feed(frame, speech=True)[0], None)
        kind, chunks = segmenter.feed(frame, speech=True)
        self.assertEqual(kind, 'start')
        self.assertEqual(len(chunks), 15)
        for _ in range(49):
            self.assertEqual(segmenter.feed(frame, speech=False)[0], 'chunk')
        self.assertEqual(segmenter.feed(frame, speech=False)[0], 'end')
        self.assertFalse(segmenter.active)

    def test_noise_and_click_not_speech(self):
        detector = Segmenter(Settings())
        for _ in range(100):
            self.assertEqual(detector.feed(bytes(1920))[0], None)
        self.assertEqual(detector.feed(bytes(1920), speech=True)[0], None)
        for _ in range(10):
            self.assertEqual(detector.feed(bytes(1920), speech=False)[0], None)

    def test_streaming_resampler_duration_and_packet_size(self):
        converter = UplinkResampler()
        pcm = (np.sin(np.arange(48000) * 2 * np.pi * 1000 / 48000) * 10000).astype('<i2').tobytes()
        packets = []
        for start in range(0, len(pcm), 1920):
            packets.extend(converter.feed(pcm[start:start + 1920]))
        packets.extend(converter.feed(b'', final=True))
        self.assertEqual(len(packets), 50)
        self.assertTrue(all(len(p) == 640 for p in packets))
        samples = np.frombuffer(b''.join(packets), dtype='<i2')
        self.assertLess(abs(np.sqrt(np.mean(samples.astype(float) ** 2)) - 7071), 100)


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Settings(log_dir=Path(self.tmp.name)), 'test')
        self.turn = self.journal.new_turn('c')

    def tearDown(self):
        if self.turn.streams:
            self.turn.finish('test')
        self.journal.close()
        self.tmp.cleanup()

    def test_asr_snapshot_replacement_and_out_of_order_completion(self):
        reply = Reply(self.turn)
        for text, interim in [('天', True), ('天气', True), ('天气如何？', False)]:
            reply.accept(decode(server_frame(451, {'results': [{'text': text, 'is_interim': interim}]})))
        self.assertEqual(self.turn.data['user_text'], '天气如何？')
        for event in (459, 359):
            reply.accept(decode(server_frame(event)))
        self.assertFalse(reply.complete)
        reply.accept(decode(server_frame(550, {'content': '晴天。'})))
        reply.accept(decode(server_frame(559)))
        self.assertTrue(reply.complete)
        self.assertEqual(self.turn.data['assistant_text_status'], 'final')

    def test_mismatched_reply_rejected(self):
        reply = Reply(self.turn)
        reply.accept(decode(server_frame(550, {'reply_id': 'a', 'content': '你好'})))
        with self.assertRaises(CloudError):
            reply.accept(decode(server_frame(550, {'reply_id': 'b', 'content': '错误'})))

    def test_reply_limit_uses_sentence_boundary(self):
        reply = Reply(self.turn)
        reply.audio = bytearray(b'\0\0' * 24000 * 25)
        reply.sentence_ends = [48000 * 5, 48000 * 15, 48000 * 25]
        data, truncated = prepare_transmission(reply, Settings())
        self.assertTrue(truncated)
        self.assertEqual(len(data), 96000 * 15)

    def test_secrets_never_written(self):
        self.journal.settings.api_key = 'secret-1234'
        self.turn.event('error', payload={'headers': 'secret-1234'})
        self.turn.finish('failed', 'token secret-1234')
        for file in self.journal.path.rglob('*'):
            if file.suffix in ('.json', '.jsonl', '.txt'):
                self.assertNotIn('secret-1234', file.read_text())


class CloudIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websocket_roundtrip_and_transcript_artifacts(self):
        received = []

        async def service(ws):
            async for message in ws:
                event, sid, data = client_frame(message)
                received.append(event)
                if event == 1:
                    await ws.send(server_frame(50, sid='connection'))
                elif event == 100:
                    config = json.loads(data)
                    self.assertEqual(config['dialog']['extra']['input_mod'], 'push_to_talk')
                    await ws.send(server_frame(150, {'dialog_id': 'dialog'}, sid))
                elif event == 200:
                    self.assertEqual(len(data), 640)
                elif event == 400:
                    events = [(451, {'results': [{'text': '你好', 'is_interim': False}]}),
                              (459, {}), (550, {'content': '你好，收到。'}), (559, {}),
                              (350, {'text': '你好，收到。'}), (352, b'\0\0' * 2400), (351, {}), (359, {})]
                    for code, payload in events:
                        await ws.send(server_frame(code, payload, sid))
                elif event == 102:
                    await ws.send(server_frame(152, sid=sid))
                elif event == 2:
                    return

        with tempfile.TemporaryDirectory() as directory:
            async with serve(service, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                settings = Settings(api_key='test-key', endpoint=f'ws://127.0.0.1:{port}', log_dir=Path(directory))
                journal = Journal(settings, 'integration')
                source = Path(directory) / 'source.wav'
                write_wav(source, b'\0\0' * 4800, 48000)
                with patch('radio_agent.engine.transmit_file') as transmitter:
                    await Engine(settings, journal).replay(source)
                    transmitter.assert_not_called()
                journal.close()
                metadata = json.loads(next(journal.path.glob('*/turn.json')).read_text())
                self.assertEqual(metadata['user_text'], '你好')
                self.assertEqual(metadata['assistant_text'], '你好，收到。')
                self.assertEqual(metadata['transmission'], 'not_requested')
                tx = next(journal.path.glob('*/tx.wav'))
                data, rate = read_wav(tx)
                self.assertEqual(rate, 48000)
                self.assertEqual(len(data), 9600)
                self.assertEqual(received.count(400), 1)
                self.assertIn(102, received)

    async def test_disconnect_does_not_transmit_and_keeps_input(self):
        async def service(ws):
            async for message in ws:
                event, sid, data = client_frame(message)
                if event == 1:
                    await ws.send(server_frame(50, sid='connection'))
                elif event == 100:
                    await ws.send(server_frame(150, {'dialog_id': 'd'}, sid))
                elif event == 200:
                    await ws.close()

        with tempfile.TemporaryDirectory() as directory:
            async with serve(service, '127.0.0.1', 0) as server:
                settings = Settings(api_key='test', endpoint=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', log_dir=Path(directory))
                journal = Journal(settings, 'test')
                source = Path(directory) / 'source.wav'
                write_wav(source, b'\0\0' * 4800, 48000)
                with patch('radio_agent.engine.verify_radio'), patch('radio_agent.engine.transmit_file') as tx:
                    with self.assertRaises(Exception):
                        await Engine(settings, journal).replay(source, transmit=True)
                    tx.assert_not_called()
                journal.close()
                metadata = json.loads(next(journal.path.glob('*/turn.json')).read_text())
                self.assertEqual(metadata['status'], 'failed')
                data, _ = read_wav(next(journal.path.glob('*/rx.wav')))
                self.assertEqual(len(data), 9600)


if __name__ == '__main__':
    unittest.main()

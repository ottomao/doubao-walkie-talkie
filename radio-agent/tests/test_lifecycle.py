import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from radio_agent.cloud import Reply, RealtimeClient
from radio_agent.engine import Engine, transmit_file
from radio_agent.journal import Journal
from radio_agent.settings import Settings


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(log_dir=Path(self.tmp.name), api_key='test-credential')
        self.journal = Journal(self.settings, 'test')

    async def asyncTearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    async def test_transmit_cancel_terminates_child(self):
        turn = self.journal.new_turn('c')
        started = asyncio.Event()
        process = MagicMock()
        process.pid = 123
        process.returncode = None

        async def communicate():
            if process.returncode is not None:
                return b'', b''
            started.set()
            await asyncio.Future()

        process.communicate = communicate
        process.terminate.side_effect = lambda: setattr(process, 'returncode', 130)
        with patch('radio_agent.engine.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(transmit_file(Path('fake.wav'), 1, turn))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        turn.finish('interrupted')

    async def test_timeout_preserves_partial_text_and_prevents_transmit(self):
        turn = self.journal.new_turn('c')
        client = MagicMock()
        client.finish_input = AsyncMock(side_effect=asyncio.TimeoutError)
        engine = Engine(self.settings, self.journal, client)
        turn.data['user_text'] = '你好'
        turn.data['user_text_status'] = 'final'
        with patch('radio_agent.engine.transmit_file', AsyncMock()) as transmit:
            with self.assertRaises(asyncio.TimeoutError):
                await engine.respond(turn, transmit=True)
            transmit.assert_not_called()
        turn.finish('failed', 'reply_timeout')
        self.assertEqual(turn.data['user_text'], '你好')
        self.assertEqual(turn.data['transmission'], 'not_started')

    async def test_continuous_loop_runs_one_turn_then_releases_capture(self):
        capture = MagicMock()
        capture.open.return_value = capture
        capture.read = AsyncMock(return_value=bytes(1920))
        detector = MagicMock()
        detector.feed.side_effect = [('start', [bytes(1920)] * 8), ('end', [bytes(1920)])]
        client = MagicMock()
        client.ws = object()
        client.close = AsyncMock()
        client.send_audio = AsyncMock()
        turns = []

        async def begin(turn):
            turns.append(turn)

        async def finish():
            reply = Reply(turns[0])
            reply.audio = bytearray(bytes(4800))
            return reply

        client.begin = begin
        client.finish_input = finish
        with patch('radio_agent.engine.verify_radio'), \
                patch('radio_agent.engine.Capture', return_value=capture), \
                patch('radio_agent.engine.Segmenter', return_value=detector), \
                patch('radio_agent.engine.transmit_file', AsyncMock(return_value={'underflows': 0})) as tx:
            await Engine(self.settings, self.journal, client).run(once=True)
        tx.assert_awaited_once()
        capture.set_accepting.assert_any_call(False)
        capture.close.assert_called_once()
        client.close.assert_awaited_once()
        self.assertEqual(turns[0].data['transmission'], 'completed')

    async def test_reset_discards_cloud_context(self):
        client = MagicMock()
        client.close = AsyncMock()
        engine = Engine(self.settings, self.journal, client)
        before = engine.conversation_id
        await engine.reset('requested')
        self.assertNotEqual(engine.conversation_id, before)
        client.close.assert_awaited_once_with(reset=True)

    async def test_failed_transmit_retries_release(self):
        turn = self.journal.new_turn('c')
        failed = MagicMock(returncode=1, pid=123)
        failed.communicate = AsyncMock(return_value=(b'', b'Device not configured'))
        release = MagicMock(returncode=0)
        release.communicate = AsyncMock(return_value=(b'', b''))
        with patch('radio_agent.engine.asyncio.create_subprocess_exec',
                   AsyncMock(side_effect=[failed, release])) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'Transmit failed'):
                await transmit_file(Path('fake.wav'), 1, turn)
        self.assertEqual(spawn.await_count, 2)
        self.assertIn('release', spawn.call_args.args)
        turn.finish('failed')

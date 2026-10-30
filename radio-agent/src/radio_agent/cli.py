import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import tempfile

from .settings import Settings
from .journal import Journal
from .engine import Engine, listen_only, verify_radio

DEFAULT_ENV = Path(__file__).resolve().parents[2] / '.env'


async def execute(args, settings, journal):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    if args.command == 'listen':
        await listen_only(settings, journal, args.seconds)
        return
    engine = Engine(settings, journal)
    loop.add_signal_handler(signal.SIGHUP, lambda: setattr(engine, 'reset_requested', True))
    if args.command == 'run':
        if args.seconds:
            try:
                async with asyncio.timeout(args.seconds):
                    await engine.run(args.once)
            except TimeoutError:
                journal.emit('run_duration_reached')
        else:
            await engine.run(args.once)
    else:
        await engine.replay(args.path, args.transmit)


def main():
    parser = argparse.ArgumentParser(description='AIOC ↔ Volcengine half-duplex voice agent')
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Check radio configuration and credential presence, without cloud calls')
    listen = commands.add_parser('listen', help='Test local voice detection and recordings; no cloud or PTT')
    listen.add_argument('--seconds', type=float, default=10)
    run = commands.add_parser('run', help='Keep listening and reply over radio')
    run.add_argument('--once', action='store_true', help='Stop after one conversation turn')
    run.add_argument('--seconds', type=float, help='Stop after a bounded smoke test')
    replay = commands.add_parser('replay', help='Send WAV to cloud; save text and audio; no PTT by default')
    replay.add_argument('path', type=Path)
    replay.add_argument('--transmit', action='store_true', help='Also transmit the generated reply over AIOC')
    args = parser.parse_args()
    journal = None
    settings = None
    lock = None
    try:
        settings = Settings.load(args.env_file)
        if getattr(args, 'seconds', None) is not None:
            import math
            if not math.isfinite(args.seconds) or args.seconds <= 0:
                raise ValueError('--seconds must be positive and finite')
        if args.command == 'doctor':
            result = verify_radio()
            result['credentials_present'] = bool(settings.api_key or (settings.app_id and settings.access_key))
            result['env_file'] = str(args.env_file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command != 'listen':
            settings.require_credentials()
        # One application process per user, regardless of chosen log directory.
        lock_path = Path(tempfile.gettempdir()) / f'walkie-radio-agent-{os.getuid()}.lock'
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(descriptor, 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another radio-agent process is running') from None
        journal = Journal(settings, args.command)
        asyncio.run(execute(args, settings, journal))
        journal.emit('run_finished')
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        if journal:
            journal.emit('run_interrupted')
        return 130
    except Exception as exc:
        error = settings.redact(exc) if settings else str(exc)
        if journal:
            journal.emit('run_failed', error=error)
        else:
            print(json.dumps(dict(ok=False, error=error), ensure_ascii=False))
        return 1
    finally:
        if journal:
            journal.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())

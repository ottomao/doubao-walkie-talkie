import argparse
import json
import signal
import sys

from . import device


def main():
    parser = argparse.ArgumentParser(description='AIOC USB radio control (macOS)')
    parser.add_argument('--json', action='store_true', help='Machine-readable output')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('devices', help='List AIOC serial and audio interfaces')
    commands.add_parser('audio-check', help='Measure two seconds of silent playback without asserting PTT')
    config = commands.add_parser('config', help='Read HID PTT configuration')
    config.add_argument('--serial')
    config.add_argument('--serial-ptt', action='store_true', help='Disable AutoPTT and enable serial PTT in RAM only')
    doctor = commands.add_parser('doctor', help='Check serial release and audio formats without transmitting')
    doctor.add_argument('--port')
    off = commands.add_parser('release', help='Explicitly clear serial PTT')
    off.add_argument('--port')
    ptt = commands.add_parser('ptt', help='Key transmitter for a bounded time, then release')
    ptt.add_argument('--seconds', type=float, default=1)
    ptt.add_argument('--port')
    for name in ('record', 'listen'):
        sub = commands.add_parser(name)
        if name == 'record':
            sub.add_argument('path')
        sub.add_argument('--seconds', type=float, default=5)
        sub.add_argument('--input', type=int)
    tx = commands.add_parser('transmit', help='Transmit a mono 16-bit 48 kHz WAV (max 60 seconds)')
    tx.add_argument('path')
    tx.add_argument('--port')
    tx.add_argument('--output', type=int)
    tx.add_argument('--gain', type=float, default=0.25)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == 'audio-check':
            result = device.check_playback()
        elif args.command == 'config':
            from .config import inspect
            result = inspect(args.serial, args.serial_ptt)
        elif args.command == 'devices':
            result = device.inventory()
        elif args.command == 'doctor':
            result = device.inventory()
            with device.control(args.port) as connection:
                result['serial_control'] = dict(port=connection.port, dtr=False, rts=False)
            for direction in ('input', 'output'):
                index = device.audio_device(direction)
                check = getattr(device.sd, f'check_{direction}_settings')
                check(device=index, channels=1, samplerate=48000, dtype='int16')
            result['audio_format'] = 'mono PCM 16-bit 48000 Hz supported'
        elif args.command == 'release':
            with device.control(args.port):
                pass
            result = dict(ptt='released')
        elif args.command == 'ptt':
            result = device.ptt(args.seconds, args.port)
        elif args.command == 'record':
            result = device.record(args.path, args.seconds, args.input)
        elif args.command == 'listen':
            result = device.listen(args.seconds, args.input)
        else:
            result = device.transmit(args.path, args.port, args.output, args.gain)
        print(json.dumps(dict(ok=True, result=result), ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps(dict(ok=False, error='Interrupted; serial PTT cleanup attempted')), file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc)), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())

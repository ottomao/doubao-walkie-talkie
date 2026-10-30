"""Volcengine realtime v3 binary frames, per official API documentation."""
from dataclasses import dataclass
import gzip
import json
import struct

MAX_PAYLOAD = 8 * 1024 * 1024


@dataclass
class Event:
    event: int
    payload: dict | bytes
    session_id: str = ''
    error_code: int | None = None


def encode(event, payload=None, session_id=''):
    audio = isinstance(payload, bytes)
    data = payload if audio else json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':')).encode()
    frame = bytes((0x11, 0x24 if audio else 0x14, 0 if audio else 0x10, 0))
    frame += struct.pack('>I', event)
    if event >= 100:
        if not session_id:
            raise ValueError('Session event requires a session ID')
        sid = session_id.encode()
        frame += struct.pack('>I', len(sid)) + sid
    return frame + struct.pack('>I', len(data)) + data


def decode(frame):
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise ValueError('Expected a binary protocol frame')
    version, header_size = frame[0] >> 4, (frame[0] & 15) * 4
    kind, flags = frame[1] >> 4, frame[1] & 15
    serialization, compression = frame[2] >> 4, frame[2] & 15
    if version != 1 or header_size < 4 or header_size > len(frame):
        raise ValueError('Invalid frame header')
    if kind not in (9, 11, 15) or serialization not in (0, 1) or compression not in (0, 1):
        raise ValueError('Unsupported frame format')
    pos = header_size

    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(frame):
            raise ValueError('Truncated protocol frame')
        data = frame[pos:pos + n]
        pos += n
        return data

    def number():
        return struct.unpack('>I', take(4))[0]

    code = number() if kind == 15 else None
    if flags & 1:
        take(4)  # optional sequence
    event = number() if flags & 4 else 0
    sid = ''
    if event >= 100 or event in (50, 51, 52):
        sid = take(number()).decode()
    size = number()
    if size > MAX_PAYLOAD:
        raise ValueError('Payload too large')
    data = take(size)
    if pos != len(frame):
        raise ValueError('Unexpected trailing protocol bytes')
    if compression:
        # Bounded decompression avoids allocating arbitrary compressed payloads.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as zipped:
            data = zipped.read(MAX_PAYLOAD + 1)
        if len(data) > MAX_PAYLOAD:
            raise ValueError('Decompressed payload too large')
    payload = json.loads(data) if serialization == 1 else data
    if serialization == 1 and not isinstance(payload, dict):
        raise ValueError('Expected JSON object')
    return Event(event, payload, sid, code)

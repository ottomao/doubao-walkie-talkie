"""AIOC HID configuration. Changes are volatile unless explicitly stored externally."""
import struct
import hid

REGISTERS = {'magic': 0, 'ptt1_sources': 0x24, 'ptt2_sources': 0x25}


def read(device, address):
    device.send_feature_report(struct.pack('<BBBI', 0, 0, address, 0))
    data = bytes(device.get_feature_report(0, 7))
    if len(data) != 7 or data[:3] != bytes((0, 0, address)):
        raise RuntimeError(f'Unexpected HID response: {data.hex()}')
    return struct.unpack('<BBBI', data)[3]


def inspect(serial_number=None, serial_ptt=False):
    devices = hid.enumerate(0x1209, 0x7388)
    matches = [d for d in devices if serial_number is None or d['serial_number'] == serial_number]
    if len(matches) != 1:
        raise ValueError('Select exactly one AIOC with --serial')
    device = hid.device()
    device.open_path(matches[0]['path'])
    try:
        values = {name: read(device, addr) for name, addr in REGISTERS.items()}
        if values['magic'] != int.from_bytes(b'AIOC', 'little'):
            raise RuntimeError('AIOC configuration signature mismatch')
        result = dict(serial=matches[0]['serial_number'], registers=values,
                      auto_ptt=bool(values['ptt1_sources'] & 0x1000),
                      serial_ptt=bool(values['ptt1_sources'] & 0x400))
        if serial_ptt:
            # Clear only AutoPTT, enable DTR & !RTS, preserve other sources.
            value = (values['ptt1_sources'] & ~0x1000) | 0x400
            device.send_feature_report(struct.pack('<BBBI', 0, 1, 0x24, value))
            if read(device, 0x24) != value:
                raise RuntimeError('PTT configuration verification failed')
            result.update(previous_ptt1_sources=values['ptt1_sources'],
                          current_ptt1_sources=value, persistent=False)
        return result
    finally:
        device.close()

import unittest
from unittest.mock import MagicMock, patch
import struct
from aioc import config


class ConfigTests(unittest.TestCase):
    def test_switch_preserves_other_sources_without_flash_write(self):
        device = MagicMock()
        values = [int.from_bytes(b'AIOC', 'little'), 0x1004, 8, 0x404]
        with patch.object(config.hid, 'enumerate', return_value=[dict(serial_number='test', path=b'test')]), \
                patch.object(config.hid, 'device', return_value=device), \
                patch.object(config, 'read', side_effect=values):
            result = config.inspect(serial_ptt=True)
        device.send_feature_report.assert_called_once_with(struct.pack('<BBBI', 0, 1, 0x24, 0x404))
        self.assertFalse(result['persistent'])
        device.close.assert_called_once()

    def test_malformed_response_rejected(self):
        device = MagicMock()
        device.get_feature_report.return_value = [0] * 7
        with self.assertRaises(RuntimeError):
            config.read(device, 0x24)

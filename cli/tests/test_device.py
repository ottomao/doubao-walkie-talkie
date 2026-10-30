import unittest
from unittest.mock import patch, MagicMock

from aioc import device


class ControlTests(unittest.TestCase):
    def connection(self):
        connection = MagicMock()
        connection.is_open = True
        return connection

    def test_releases_after_interruption(self):
        connection = self.connection()
        with patch.object(device.serial, 'Serial', return_value=connection), \
                patch.object(device, 'serial_device', return_value='/dev/fake'), \
                patch.object(device.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                device.ptt(1)
        self.assertFalse(connection.dtr)
        self.assertFalse(connection.rts)
        connection.close.assert_called_once()

    def test_invalid_duration_never_opens_serial(self):
        with patch.object(device.serial, 'Serial') as serial:
            for value in (0, -1, 61, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    device.ptt(value)
            serial.assert_not_called()

    def test_multiple_boards_require_selection(self):
        with patch.object(device, 'inventory', return_value={
            'serial_devices': [{'port': '/dev/a'}, {'port': '/dev/b'}]
        }):
            with self.assertRaises(ValueError):
                device.serial_device()
            self.assertEqual(device.serial_device('/dev/b'), '/dev/b')

    def test_never_falls_back_to_system_microphone(self):
        with patch.object(device.sd, 'query_devices', return_value=[
            dict(name='Mac microphone', max_input_channels=1, max_output_channels=0)
        ]):
            with self.assertRaises(ValueError):
                device.audio_device('input')

    def test_audio_failure_releases_ptt(self):
        import tempfile
        import wave
        connection = self.connection()
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.write.side_effect = RuntimeError('USB disconnected')
        with tempfile.NamedTemporaryFile(suffix='.wav') as file:
            with wave.open(file.name, 'wb') as wav:
                wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0' * 480)
            with patch.object(device.serial, 'Serial', return_value=connection), \
                    patch.object(device, 'serial_device', return_value='/dev/fake'), \
                    patch.object(device, 'audio_device', return_value=1), \
                    patch.object(device, 'ConnectionGuard'), \
                    patch.object(device.sd, 'OutputStream', return_value=stream), \
                    patch.object(device.time, 'sleep'):
                with self.assertRaises(RuntimeError):
                    device.transmit(file.name)
        self.assertFalse(connection.dtr)
        connection.close.assert_called_once()

    def test_replug_same_path_is_rejected(self):
        with patch.object(device, 'serial_device', return_value='/dev/fake'), \
                patch.object(device.os, 'stat', side_effect=[
                    MagicMock(st_ino=1, st_ctime_ns=10),
                    MagicMock(st_ino=2, st_ctime_ns=20)]):
            guard = device.ConnectionGuard()
            with self.assertRaises(device.DeviceDisconnected):
                guard.check()

    def test_close_even_when_release_fails(self):
        connection = self.connection()
        with patch.object(device.serial, 'Serial', return_value=connection), \
                patch.object(device, 'serial_device', return_value='/dev/fake'):
            with self.assertRaises(OSError):
                with device.control():
                    from unittest.mock import PropertyMock
                    type(connection).dtr = PropertyMock(side_effect=OSError('removed'))
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()

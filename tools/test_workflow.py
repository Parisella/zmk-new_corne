"""Safety checks for firmware selection and one-shot USB flashing, using temporary files."""
import contextlib
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import flash_when_connected as flash
from check_keymap import check


def uf2(family=0xADA52840, address=0x26000):
    block = bytearray(512)
    struct.pack_into('<8I', block, 0, 0x0A324655, 0x9E5D5157, 0x2000, address, 256, 0, 1, family)
    struct.pack_into('<I', block, 508, 0x0AB16F30)
    return bytes(block)


class WorkflowTests(unittest.TestCase):
    def test_wrong_chip_and_bootloader_write_are_rejected(self):
        for data in [uf2(0xE48BFF56), uf2(address=0), uf2(address=0xEC000), b'invalid']:
            with self.subTest(data=data[:32]), self.assertRaises(ValueError):
                flash.validate_uf2(data)
        flash.validate_uf2(uf2())

    def make_manifest(self, directory):
        data = uf2()
        (directory / 'eyelash_corne_left.uf2').write_bytes(data)
        manifest = {'repository': 'Parisella/zmk-new_corne', 'commit': 'a' * 40,
                    'files': [{'name': 'eyelash_corne_left.uf2', 'half': 'left', 'variant': 'standard',
                               'sha256': hashlib.sha256(data).hexdigest()}]}
        path = directory / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return path

    def test_wrong_half_variant_and_modified_image_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_manifest(root)
            for half, variant in [('right', 'standard'), ('left', 'studio'), ('left', 'settings-reset')]:
                with self.subTest(half=half, variant=variant), self.assertRaises(ValueError):
                    flash.select_firmware(manifest, half, variant)
            (root / 'eyelash_corne_left.uf2').write_bytes(uf2(address=0x27000))
            with self.assertRaises(ValueError):
                flash.select_firmware(manifest, 'left', 'standard')

    def test_new_matching_device_transfers_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_manifest(root)
            device = root / 'device'
            device.mkdir()
            scans = [{}, {str(device): {'Board-ID': 'test-nrf52840', 'Model': 'nRF52840'}}, {}]
            args = ['flash', '--manifest', str(manifest), '--half', 'left', '--board-id', 'test-nrf52840']
            with patch.object(sys, 'argv', args), patch.object(flash, 'bootloaders', side_effect=scans), contextlib.redirect_stdout(io.StringIO()):
                flash.main()
            self.assertEqual((device / 'firmware.uf2').read_bytes(), uf2())
            logs = list(root.glob('flash-left-*.json'))
            self.assertEqual(len(logs), 1)
            self.assertEqual(json.loads(logs[0].read_text())['status'], 'transfer-complete-bootloader-disconnected')

    def test_unexpected_device_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.make_manifest(root)
            device = root / 'device'
            device.mkdir()
            scans = [{}, {str(device): {'Board-ID': 'wrong-nrf52840'}}]
            args = ['flash', '--manifest', str(manifest), '--half', 'left', '--board-id', 'expected-nrf52840']
            with patch.object(sys, 'argv', args), patch.object(flash, 'bootloaders', side_effect=scans), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                flash.main()
            self.assertFalse((device / 'firmware.uf2').exists())

    def test_duplicate_combo_on_same_layer_is_rejected(self):
        source = Path(__file__).resolve().parents[1] / 'config/eyelash_corne.keymap'
        text = source.read_text().replace('        Home {', '        duplicate { bindings = <&kp HOME>; key-positions = <23 24 25>; };\n        Home {')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.keymap'
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'identical triggers'):
                check(path)


if __name__ == '__main__':
    unittest.main()

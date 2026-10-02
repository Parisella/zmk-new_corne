"""Wait for a newly connected, explicitly identified nRF52840 UF2 bootloader."""
import argparse
import ctypes
import hashlib
import json
import os
import re
import struct
import time
from pathlib import Path


def bootloaders():
    if os.name != 'nt':
        raise RuntimeError('This drive watcher is for Windows.')
    drives = {}
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = Path(chr(65 + index) + ':\\')
        if ctypes.windll.kernel32.GetDriveTypeW(str(root)) not in (2, 3):
            continue
        try:
            text = (root / 'INFO_UF2.TXT').read_text(errors='replace')
            fields = dict(re.findall(r'^([^:\r\n]+):\s*(.*?)\s*$', text, re.M))
            if fields.get('Board-ID'):
                drives[str(root)] = fields
        except OSError:
            pass
    return drives


def validate_uf2(data):
    if not data or len(data) % 512:
        raise ValueError('Invalid UF2 length.')
    expected = len(data) // 512
    for index in range(expected):
        block = data[index * 512:(index + 1) * 512]
        magic0, magic1, flags, address, size, number, count, family = struct.unpack_from('<8I', block)
        if (magic0, magic1, struct.unpack_from('<I', block, 508)[0]) != (0x0A324655, 0x9E5D5157, 0x0AB16F30):
            raise ValueError('Invalid UF2 block magic.')
        if flags != 0x2000 or family != 0xADA52840:
            raise ValueError('Expected ordinary nRF52840 firmware blocks.')
        if number != index or count != expected or not 0 < size <= 476:
            raise ValueError('Invalid UF2 block sequence or payload.')
        if address < 0x26000 or address + size > 0xEC000:
            raise ValueError('Image writes outside the Eyelash Corne application partition.')


def select_firmware(manifest_path, half, variant):
    manifest = json.loads(manifest_path.read_text())
    if manifest['repository'] != 'Parisella/zmk-new_corne' or not re.fullmatch('[0-9a-f]{40}', manifest['commit']):
        raise ValueError('Unexpected build manifest.')
    matches = [f for f in manifest['files'] if f['half'] == half and f['variant'] == variant]
    if len(matches) != 1:
        raise ValueError('Expected exactly one artifact for the selected half and variant.')
    item = matches[0]
    if Path(item['name']).name != item['name']:
        raise ValueError('Invalid artifact filename.')
    data = (manifest_path.parent / item['name']).read_bytes()
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Firmware hash differs from the downloaded build.')
    validate_uf2(data)
    return manifest, item, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='List bootloaders without writing anything')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--half', choices=['left', 'right'])
    parser.add_argument('--variant', choices=['standard', 'studio'], default='standard')
    parser.add_argument('--board-id', help='Exact Board-ID enrolled from INFO_UF2.TXT')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--dry-run', action='store_true', help='Validate firmware only; never wait or write')
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(bootloaders(), indent=2))
        return
    if not args.manifest or not args.half:
        parser.error('--manifest and --half are required')
    manifest, item, data = select_firmware(args.manifest.resolve(), args.half, args.variant)
    if args.dry_run:
        print(f"Validated {item['name']} from {manifest['commit']}; no device writes.")
        return
    if not args.board_id:
        parser.error('Enroll and provide the exact --board-id before arming automatic flashing')
    if args.timeout < 1 or args.timeout > 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    before = bootloaders()
    print(f"ARMED for {args.half} half, {item['name']}, commit {manifest['commit']}. Connect only this half and double-tap reset. Waiting {args.timeout}s for a NEW bootloader drive...", flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        current = bootloaders()
        newcomers = {drive: fields for drive, fields in current.items() if drive not in before}
        if len(newcomers) > 1:
            raise RuntimeError('Multiple new bootloaders detected; disconnect extras and re-arm.')
        for drive, fields in newcomers.items():
            if fields['Board-ID'] != args.board_id:
                raise RuntimeError(f"Unexpected bootloader Board-ID {fields['Board-ID']!r}; nothing flashed.")
            if 'nrf52840' not in json.dumps(fields).lower():
                raise RuntimeError('Bootloader does not identify an nRF52840; nothing flashed.')
            log_path = args.manifest.resolve().parent / f'flash-{args.half}-{time.time_ns()}.json'
            record = {'half': args.half, 'firmware': item, 'commit': manifest['commit'], 'drive': drive,
                      'bootloader': fields, 'status': 'writing'}
            log_path.write_text(json.dumps(record, indent=2))
            print('Verified bootloader. Transferring firmware once...', flush=True)
            try:
                with (Path(drive) / 'firmware.uf2').open('wb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as error:
                record['status'] = 'transfer-uncertain'
                log_path.write_text(json.dumps(record, indent=2))
                raise RuntimeError('Drive disappeared or write failed during transfer. Inspect keyboard before retrying; no automatic retry.') from error
            record['status'] = 'transfer-complete'
            reboot_deadline = time.monotonic() + 10
            while time.monotonic() < reboot_deadline:
                if drive not in bootloaders():
                    record['status'] = 'transfer-complete-bootloader-disconnected'
                    break
                time.sleep(0.5)
            log_path.write_text(json.dumps(record, indent=2))
            print(record['status'] + '; verify key behavior after reboot. Log: ' + str(log_path), flush=True)
            return
        # Drives present when armed must disappear before they can qualify as new.
        before = {drive: fields for drive, fields in before.items() if drive in current}
        time.sleep(0.5)
    raise RuntimeError('Timed out; nothing flashed. Re-arm when ready.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error))

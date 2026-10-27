#!/usr/bin/python3
"""REAL FUSE/NBD test for the normal host. Writes only to a converted copy."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hdd_core import Manager, digest, run


def main():
    if not Path('/dev/fuse').exists():
        raise RuntimeError('Test unavailable: /dev/fuse is missing. Run on the normal host.')
    if len(sys.argv) != 2:
        raise RuntimeError('Usage: test_mount.py PATH_TO_QCOW2_IMAGE')
    source = Path(sys.argv[1]).resolve(strict=True)
    base = Path(__file__).resolve().parent.parent
    if not (base / 'bin/fatxfs').is_file():
        raise RuntimeError('Run this test from the installed application directory.')
    # Store under the app to keep all data, mounts and temporary backups in one folder.
    work = Path(tempfile.mkdtemp(prefix='mount-test-', dir=base))
    manager = None
    try:
        original_hash = digest(source)
        (work / 'bin').mkdir()
        shutil.copy2(base / 'bin/fatxfs', work / 'bin/fatxfs')
        shutil.copy2(base / 'root_helper.py', work / 'root_helper.py')
        copy = work / 'test.qcow2'
        run(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', source, copy])
        manager = Manager(work)
        if manager.state():
            raise RuntimeError('A disk is already connected: unmount it before testing.')
        # Each connection needs normal pkexec authentication.
        manager.mount(copy, False)
        directory = manager.mounts / 'E/_HDD_Manager_Test'
        directory.mkdir()
        keep, delete = directory / 'keep.bin', directory / 'delete.bin'
        data = bytes(range(251)) * 1200
        with keep.open('wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        delete.write_bytes(b'delete me')
        delete.unlink()
        manager.unmount()
        manager.mount(copy, False)
        assert keep.read_bytes() == data, 'Data did not persist after unmount'
        assert not delete.exists(), 'Deletion did not persist'
        renamed = directory / 'renamed.bin'
        keep.rename(renamed)
        with renamed.open('r+b') as f:
            f.seek(16380)
            f.write(b'PERSISTENT EDIT')
            f.truncate(100003)
            f.flush()
            os.fsync(f.fileno())
        manager.unmount()
        manager.mount(copy, True)
        expected = bytearray(data[:100003])
        expected[16380:16380 + len(b'PERSISTENT EDIT')] = b'PERSISTENT EDIT'
        assert renamed.read_bytes() == bytes(expected), 'Edit or truncation did not persist'
        assert not keep.exists() and not delete.exists(), 'Rename or deletion did not persist'
        try:
            renamed.write_bytes(b'forbidden')
        except OSError:
            pass
        else:
            raise AssertionError('Read-only mode did not block writes')
        manager.unmount()
        run(['qemu-img', 'check', '-f', 'qcow2', copy])
        assert digest(source) == original_hash, 'Original hash changed during the test'
        print('PASS: read/write mount, fsync, unmount, reopen, edit, rename, deletion and read-only mode. Original unchanged.')
    finally:
        safe = True
        if manager:
            try:
                current = manager.state()
                if current and current.get('image') == str(work / 'test.qcow2'):
                    manager.unmount()
                elif manager.mounted():
                    raise RuntimeError('Test mounts have no recognized state.')
            except Exception as exc:
                safe = False
                print(f'Cleanup incomplete: {exc}\nTest data retained in {work}', file=sys.stderr)
            manager.lock.close()
        if safe:
            shutil.rmtree(work)


if __name__ == '__main__':
    main()

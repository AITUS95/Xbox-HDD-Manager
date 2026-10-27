from pathlib import Path
import fcntl
import json
import tempfile
import unittest
from unittest.mock import patch
import update
from hdd_core import layout_paths

SOURCE = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'install with spaces'
        (self.target / 'bin').mkdir(parents=True)
        (self.target / 'bin/fatxfs').write_text('previous driver')
        (self.target / 'hdd_core.py').write_text('previous code')
        (self.target / 'OldMounts').mkdir()
        (self.target / 'OldBackups').mkdir()
        (self.target / 'OldBackups/disk.qcow2').write_bytes(b'backup data')
        (self.target / 'old-images.json').write_text('["/example/disk.qcow2"]')
        self.layout = {'mounts': 'OldMounts', 'backups': 'OldBackups', 'images': 'old-images.json'}

    def test_real_build_and_existing_data_preserved(self):
        update.update(SOURCE, self.target, self.layout)
        self.assertEqual((self.target / 'OldBackups/disk.qcow2').read_bytes(), b'backup data')
        self.assertEqual(json.loads((self.target / 'old-images.json').read_text()), ['/example/disk.qcow2'])
        self.assertEqual(layout_paths(self.target), [self.target / 'OldMounts', self.target / 'OldBackups', self.target / 'old-images.json'])
        self.assertNotEqual((self.target / 'bin/fatxfs').read_bytes(), b'previous driver')
        archive = next((self.target / 'Previous').glob('update-*'))
        self.assertEqual((archive / 'hdd_core.py').read_text(), 'previous code')
        self.assertEqual((archive / 'bin/fatxfs').read_text(), 'previous driver')

    def test_running_app_blocks_update(self):
        with (self.target / 'manager.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'Close'):
                update.update(SOURCE, self.target, self.layout)
        self.assertEqual((self.target / 'hdd_core.py').read_text(), 'previous code')

    def test_connected_image_blocks_update(self):
        with patch.object(update.Path, 'exists', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'disconnect'):
                update.update(SOURCE, self.target, self.layout)
        self.assertEqual((self.target / 'hdd_core.py').read_text(), 'previous code')

    def test_path_traversal_rejected(self):
        for name in ('../other', '/outside', '.', '..', ''):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Invalid layout'):
                update.update(SOURCE, self.target, {**self.layout, 'backups': name})

    def test_layout_defaults_and_invalid_values(self):
        self.assertEqual(layout_paths(self.target), [self.target / 'Mounts', self.target / 'Backups', self.target / 'images.json'])
        (self.target / 'layout.json').write_text('{"mounts": "../other"}')
        with self.assertRaisesRegex(RuntimeError, 'Invalid directory layout'):
            layout_paths(self.target)


if __name__ == '__main__':
    unittest.main()

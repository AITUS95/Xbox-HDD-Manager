import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import hdd_core as c
import root_helper as r


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.m = c.Manager(self.root)
        self.m.statefile = self.root / 'state.json'
        self.users_mock = patch.object(c, 'mount_users', return_value=[]).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.m.lock.close()
        self.tmp.cleanup()

    def state(self):
        d = {'device': '/dev/nbd7', 'image': '/test.qcow2', 'size': 8589934592, 'readonly': True}
        self.m.statefile.write_text(json.dumps(d))
        return d

    def test_single_instance(self):
        with self.assertRaises(RuntimeError):
            c.Manager(self.root)

    def test_lock_not_inherited(self):
        self.assertFalse(os.get_inheritable(self.m.lock.fileno()))
        self.assertEqual(c.run(['/bin/sh', '-c', 'printf ok']), 'ok')

    def test_missing_state(self):
        self.assertIsNone(self.m.state())

    def test_corrupt_state_not_ignored(self):
        self.m.statefile.write_text('broken')
        with self.assertRaises(ValueError):
            self.m.state()

    def test_probe_no_magic(self):
        p = self.root / 'raw'
        with p.open('wb') as f:
            f.truncate(8589934592)
        self.assertEqual(c.probe(p, 8589934592), {})

    def test_probe_all_partitions(self):
        p = self.root / 'raw'
        with p.open('wb') as f:
            f.truncate(8589934592)
            for offset, size in c.PARTITIONS.values():
                f.seek(offset)
                f.write(b'FATX')
        self.assertEqual(c.probe(p, 8589934592), c.PARTITIONS)

    def test_probe_truncated_partition(self):
        p = self.root / 'raw'
        with p.open('wb') as f:
            f.seek(c.PARTITIONS['C'][0])
            f.write(b'FATX')
        self.assertNotIn('C', c.probe(p, p.stat().st_size))

    def test_backup_spaces_unicode(self):
        p = self.root / 'example disk é.qcow2'
        p.write_bytes(b'\0' * 1024 + b'hello')
        dest = c.backup(p, self.root / 'Backups')
        self.assertEqual(c.digest(p), c.digest(dest))
        self.assertNotEqual(p.stat().st_ino, dest.stat().st_ino)

    def test_backup_unique(self):
        p = self.root / 'disk.img'
        p.write_bytes(b'abc')
        a = c.backup(p, self.root / 'Backups')
        b = c.backup(p, self.root / 'Backups')
        self.assertNotEqual(a, b)

    def test_failed_backup_removed(self):
        p = self.root / 'disk.img'
        p.write_bytes(b'abc')
        with patch.object(c, 'run', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                c.backup(p, self.root / 'Backups')
        self.assertEqual(list((self.root / 'Backups').iterdir()), [])

    def test_corrupt_backup_removed(self):
        p = self.root / 'disk.img'
        p.write_bytes(b'abc')
        with patch.object(c, 'digest', side_effect=['a', 'b']):
            with self.assertRaises(RuntimeError):
                c.backup(p, self.root / 'Backups')
        self.assertEqual(list((self.root / 'Backups').iterdir()), [])

    def test_backup_changed_source(self):
        p = self.root / 'disk.img'
        p.write_bytes(b'abc')
        def digest(_):
            p.write_bytes(b'changed')
            return 'same'
        with patch.object(c, 'digest', side_effect=digest):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                c.backup(p, self.root / 'Backups')

    def test_mountpoints_empty(self):
        self.m.check_mountpoints()
        self.assertTrue((self.m.mounts / 'C').is_dir())

    def test_mountpoints_nonempty_refused(self):
        self.m.check_mountpoints()
        (self.m.mounts / 'C/keep').write_text('data')
        with self.assertRaises(RuntimeError):
            self.m.check_mountpoints()
        self.assertEqual((self.m.mounts / 'C/keep').read_text(), 'data')

    def test_mountpoint_symlink_refused(self):
        self.m.mounts.mkdir()
        (self.m.mounts / 'C').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.m.check_mountpoints()

    def test_connect_existing_state(self):
        self.state()
        with self.assertRaisesRegex(RuntimeError, 'previous'):
            self.m.connect('/does/not/exist', False)

    def test_unmount_without_state(self):
        with patch.object(self.m, 'helper') as helper:
            self.m.unmount()
            helper.assert_not_called()

    def test_recovery_connected_without_mounts(self):
        self.state()
        with patch.object(self.m, 'helper') as helper:
            self.m.unmount()
            helper.assert_called_once_with('disconnect')

    def test_foreign_mount_no_state_refused(self):
        with patch.object(self.m, 'mounted', return_value={'/test': ('fuse', 'foreign')}):
            with self.assertRaises(RuntimeError):
                self.m.unmount()

    def test_foreign_source_never_unmounted(self):
        self.state()
        with patch.object(self.m, 'mounted', return_value={'/test': ('fuse', 'foreign')}), patch.object(c, 'run') as run:
            with self.assertRaises(RuntimeError):
                self.m.unmount()
            run.assert_not_called()

    def test_busy_mount_never_disconnects(self):
        self.state()
        with patch.object(self.m, 'mounted', return_value={'/test': ('fuse.fatxfs', '/dev/nbd7')}), patch.object(c, 'run', side_effect=RuntimeError('busy')), patch.object(self.m, 'helper') as helper:
            with self.assertRaises(RuntimeError):
                self.m.unmount()
            helper.assert_not_called()

    def test_busy_preflight_leaves_all_partitions_mounted(self):
        self.state()
        mounts = {'/test/C': ('fuse', '/dev/nbd7'), '/test/E': ('fuse', '/dev/nbd7')}
        self.users_mock.side_effect = [[], ['nautilus (PID 123)']]
        with patch.object(self.m, 'mounted', return_value=mounts), patch.object(c, 'run') as run, patch.object(self.m, 'helper') as helper:
            with self.assertRaisesRegex(RuntimeError, 'nautilus'):
                self.m.unmount()
            run.assert_not_called()
            helper.assert_not_called()

    def test_busy_exception_offers_nautilus_recovery(self):
        error = c.BusyPartitions(['E: nautilus (PID 9689)'])
        self.assertTrue(error.nautilus)
        self.assertFalse(c.BusyPartitions(['E: editor (PID 123)']).nautilus)

    def test_close_files_only_requests_normal_nautilus_quit(self):
        self.state()
        self.users_mock.return_value = ['nautilus (PID 9689)']
        with patch.object(self.m, 'mounted', return_value={'/test/E': ('fuse', '/dev/nbd7')}), patch.object(c, 'run') as run, patch.object(self.m, 'unmount', return_value='ok'):
            self.assertEqual(self.m.close_files_and_unmount(), 'ok')
            run.assert_called_once_with(['nautilus', '--quit'], timeout=15)

    def test_other_app_is_never_terminated(self):
        self.state()
        self.users_mock.return_value = ['editor (PID 123)']
        with patch.object(self.m, 'mounted', return_value={'/test/E': ('fuse', '/dev/nbd7')}), patch.object(c, 'run') as run, patch.object(self.m, 'unmount', side_effect=c.BusyPartitions(['editor'])), patch.object(c.time, 'sleep'):
            with self.assertRaises(c.BusyPartitions):
                self.m.close_files_and_unmount()
            run.assert_not_called()

    def test_async_nautilus_release_retried_normally(self):
        self.state()
        self.users_mock.return_value = ['nautilus (PID 9689)']
        with patch.object(self.m, 'mounted', return_value={'/test/E': ('fuse', '/dev/nbd7')}), patch.object(c, 'run'), patch.object(self.m, 'unmount', side_effect=[c.BusyPartitions(['nautilus']), 'ok']) as unmount, patch.object(c.time, 'sleep'):
            self.assertEqual(self.m.close_files_and_unmount(), 'ok')
            self.assertEqual(unmount.call_count, 2)

    def test_foreign_mount_blocks_nautilus_quit(self):
        self.state()
        with patch.object(self.m, 'mounted', return_value={'/test/E': ('fuse', 'foreign')}), patch.object(c, 'run') as run:
            with self.assertRaises(RuntimeError):
                self.m.close_files_and_unmount()
            run.assert_not_called()

    def test_unmount_order(self):
        self.state()
        events = []
        with patch.object(self.m, 'mounted', side_effect=[{'/test': ('fuse.fatxfs', '/dev/nbd7')}, {}]), patch.object(c, 'run', side_effect=lambda *a, **k: events.append('unmount')), patch.object(self.m, 'helper', side_effect=lambda *a: events.append('disconnect')):
            self.m.unmount()
        self.assertEqual(events, ['unmount', 'disconnect'])

    def test_no_fuse_clear_error(self):
        with patch.object(c.Path, 'exists', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '/dev/fuse'):
                self.m.mount('/does/not/exist')

    def test_mount_failure_rolls_back(self):
        (self.root / 'bin').mkdir()
        (self.root / 'bin/fatxfs').touch()
        self.m.check_mountpoints()
        state = {'device': '/fake', 'size': 8589934592, 'image': 'test'}
        with patch.object(c.Path, 'exists', return_value=True), patch.object(self.m, 'connect', return_value=state), patch.object(c, 'probe', return_value={'C': (0, 100)}), patch.object(c.subprocess, 'run', return_value=Mock(returncode=1)), patch.object(self.m, 'unmount') as unmount:
            with self.assertRaisesRegex(RuntimeError, 'Mounting C'):
                self.m.mount('test', True)
            unmount.assert_called_once()

    def test_mount_backup_failure_rolls_back(self):
        (self.root / 'bin').mkdir()
        (self.root / 'bin/fatxfs').touch()
        with patch.object(c.Path, 'exists', return_value=True), patch.object(self.m, 'connect', return_value={'image': 'test'}), patch.object(c, 'backup', side_effect=RuntimeError('no space')), patch.object(self.m, 'unmount') as unmount:
            with self.assertRaisesRegex(RuntimeError, 'no space'):
                self.m.mount('test', False)
            unmount.assert_called_once()

    def test_mount_success_single_thread_fsname_and_ro(self):
        (self.root / 'bin').mkdir()
        (self.root / 'bin/fatxfs').touch()
        self.m.check_mountpoints()
        state = {'device': '/dev/nbd7', 'size': 8589934592}
        with patch.object(c.Path, 'exists', return_value=True), patch.object(self.m, 'connect', return_value=state), patch.object(c, 'probe', return_value={'C': (0, 100)}), patch.object(self.m, 'mounted', return_value={str(self.m.mounts / 'C'): ('fuse', '/dev/nbd7')}), patch.object(c.subprocess, 'run', return_value=Mock(returncode=0)) as run, patch.object(c, 'backup') as backup:
            self.m.mount('test', True)
            args = run.call_args.args[0]
            self.assertIn('-s', args)
            self.assertIn('ro,fsname=/dev/nbd7', args)
            self.assertTrue(run.call_args.kwargs['close_fds'])
            backup.assert_not_called()

    def test_qcow_raw_info(self):
        for fmt in ['raw', 'qcow2']:
            with self.subTest(fmt=fmt):
                p = self.root / (fmt + '.img')
                c.run(['qemu-img', 'create', '-f', fmt, p, '16M'])
                self.assertEqual(c.image_info(p)['format'], fmt)

    def test_backing_rejected(self):
        base, overlay = self.root / 'base', self.root / 'overlay'
        c.run(['qemu-img', 'create', '-f', 'qcow2', base, '16M'])
        c.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', base, overlay])
        with self.assertRaises(RuntimeError):
            c.image_info(overlay)

    def test_busy_image_qemu_lock(self):
        p = self.root / 'image'
        c.run(['qemu-img', 'create', '-f', 'qcow2', p, '16M'])
        proc = subprocess.Popen(['qemu-io', '-f', 'qcow2', str(p)], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            ready = False
            for _ in range(100):
                for fd in Path(f'/proc/{proc.pid}/fd').glob('*'):
                    try:
                        ready |= os.readlink(fd) == str(p)
                    except FileNotFoundError:
                        pass
                if ready:
                    break
                time.sleep(.01)
            self.assertTrue(ready, 'QEMU writer did not open the test image')
            with self.assertRaises(RuntimeError):
                c.run(['qemu-io', '-f', 'qcow2', '-c', 'write 0 512', p])
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_mountinfo_escaped_paths(self):
        text = '1 0 1:2 / /some\\040path rw - fuse.fatxfs /dev/nbd7 rw\n'
        with patch.object(c.Path, 'read_text', return_value=text):
            self.assertEqual(c.mount_records()['/some path'], ('fuse.fatxfs', '/dev/nbd7'))

    def test_command_failure_propagated(self):
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            c.run(['/bin/sh', '-c', 'echo failure >&2; exit 2'])


class HelperTests(unittest.TestCase):
    def test_pid_reuse_never_disconnects(self):
        state = {'device': '/dev/nbd7', 'identity': {'pid': '42', 'start': 'old'}}
        with patch.object(r, 'identity', return_value={'pid': '42', 'start': 'new'}), patch.object(r, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                r.disconnect(state, Path('/unused'))
            run.assert_not_called()

    def test_open_device_never_disconnects(self):
        state = {'device': '/dev/nbd7', 'identity': {'pid': '42', 'start': '1'}}
        with patch.object(r, 'identity', return_value=state['identity']), patch.object(r, 'busy', return_value=True), patch.object(r, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'still open'):
                r.disconnect(state, Path('/unused'))
            run.assert_not_called()

    def test_no_pid_nonzero_size_refused(self):
        with patch.object(r, 'identity', return_value=None), patch.object(r, 'read', return_value='100'):
            with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
                r.disconnect({'device': '/dev/nbd7'}, Path('/unused'))

    def test_root_gate(self):
        with patch.object(r.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'pkexec'):
                r.main()

    def test_root_without_pkexec_uid_refused(self):
        with patch.object(r.os, 'geteuid', return_value=0), patch.dict(r.os.environ, {'PKEXEC_UID': '-1'}):
            with self.assertRaises(RuntimeError):
                r.main()

    def test_state_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            r.save(p, {'hello': 'world'})
            self.assertEqual(json.loads(p.read_text()), {'hello': 'world'})
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)
            self.assertFalse(p.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()

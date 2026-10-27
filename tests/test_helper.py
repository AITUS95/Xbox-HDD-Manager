import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import root_helper as r


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.image = self.base / 'example disk.qcow2'
        self.image.write_bytes(b'test')
        self.state = self.base / 'state.json'
        self.uid = self.image.stat().st_uid
        self.info = {'format': 'qcow2', 'virtual-size': 17179869184}
        self.calls = []
        self.acl = 'user::rw-\ngroup::rw-\nother::---\n'

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, args):
        self.calls.append(args)
        if args[0] == 'qemu-img':
            return json.dumps(self.info)
        if args[0] == 'getfacl':
            return self.acl
        return ''

    def contexts(self):
        # Connections use a 16 GiB image, deliberately different from the old 8 GiB assumption.
        return [patch.object(r, 'run', side_effect=self.execute),
                patch.object(r.subprocess, 'run', return_value=Mock(returncode=1)),
                patch.object(r, 'free_devices', return_value=iter(['/dev/nbd3'])),
                patch.object(r, 'identity', return_value={'pid': '45', 'start': '123'}),
                patch.object(r, 'read', return_value=str(self.info['virtual-size'] // 512))]

    def test_dynamic_size_device_acl_and_state(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self.contexts(): stack.enter_context(context)
            result = r.connect(self.image, 'rw', self.uid, self.state)
        self.assertEqual(result['device'], '/dev/nbd3')
        self.assertEqual(result['size'], 17179869184)
        self.assertEqual(json.loads(self.state.read_text())['identity']['start'], '123')
        self.assertIn(['setfacl', '-m', f'u:{self.uid}:rw', '/dev/nbd3'], self.calls)
        self.assertIn('--format=qcow2', next(a for a in self.calls if a[0] == 'qemu-nbd'))

    def test_existing_session_refused(self):
        self.state.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'registered'):
            r.connect(self.image, 'rw', self.uid, self.state)

    def test_owner_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'owned by'):
            r.connect(self.image, 'rw', self.uid + 1, self.state)

    def test_mode_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid mode'):
            r.connect(self.image, 'invalid', self.uid, self.state)

    def test_qemu_connection_failure_no_acl_grant(self):
        from contextlib import ExitStack
        def execute(args):
            if args[0] == 'qemu-nbd':
                raise RuntimeError('device occupied')
            return self.execute(args)
        with ExitStack() as stack:
            for context in self.contexts()[1:]: stack.enter_context(context)
            stack.enter_context(patch.object(r, 'run', side_effect=execute))
            with self.assertRaisesRegex(RuntimeError, 'occupied'):
                r.connect(self.image, 'rw', self.uid, self.state)
        self.assertFalse(any(a[0] == 'setfacl' for a in self.calls))

    def test_no_free_nbd(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in self.contexts(): stack.enter_context(context)
            stack.enter_context(patch.object(r, 'free_devices', return_value=iter([])))
            with self.assertRaisesRegex(RuntimeError, 'free'):
                r.connect(self.image, 'rw', self.uid, self.state)

    def test_image_in_use_rejected_before_connect(self):
        with patch.object(r, 'run', side_effect=self.execute), patch.object(r.subprocess, 'run', return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(RuntimeError, 'in use'):
                r.connect(self.image, 'rw', self.uid, self.state)
        self.assertFalse(any(a[0] == 'qemu-nbd' for a in self.calls))

    def test_fuser_error_not_treated_as_idle(self):
        with patch.object(r, 'run', side_effect=self.execute), patch.object(r.subprocess, 'run', return_value=Mock(returncode=2)):
            with self.assertRaisesRegex(RuntimeError, 'Unable to check'):
                r.connect(self.image, 'rw', self.uid, self.state)

    def test_backing_rejected(self):
        self.info['backing-filename'] = '/other.qcow2'
        with patch.object(r, 'run', side_effect=self.execute):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported format'):
                r.connect(self.image, 'rw', self.uid, self.state)

    def test_disconnect_acl_restored_before_state_removed(self):
        self.state.write_text('{}')
        state = {'device': '/dev/nbd3', 'identity': {'pid': '45', 'start': '123'}, 'acl': self.acl}
        with patch.object(r, 'identity', side_effect=[state['identity'], None]), patch.object(r, 'busy', return_value=False), patch.object(r, 'read', return_value='0'), patch.object(r, 'run', side_effect=self.execute), patch.object(r.Path, 'exists', return_value=True), patch.object(r.subprocess, 'run', return_value=Mock(returncode=0)) as restore:
            r.disconnect(state, self.state)
            self.assertEqual(restore.call_args.kwargs['input'], self.acl)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [['blockdev', '--flushbufs', '/dev/nbd3'], ['qemu-nbd', '--disconnect', '/dev/nbd3']])

    def test_failed_flush_keeps_session(self):
        self.state.write_text('{}')
        state = {'device': '/dev/nbd3', 'identity': {'pid': '45', 'start': '123'}}
        with patch.object(r, 'identity', return_value=state['identity']), patch.object(r, 'busy', return_value=False), patch.object(r, 'run', side_effect=RuntimeError('flush failed')):
            with self.assertRaisesRegex(RuntimeError, 'flush'):
                r.disconnect(state, self.state)
        self.assertTrue(self.state.exists())

    def test_backend_own_device_fd_is_not_busy(self):
        def text(path):
            return 'Tgid:\t100\n' if str(path).endswith('/status') else ''
        with patch.object(r.Path, 'read_text', autospec=True, side_effect=text), patch.object(r.os, 'stat', return_value=Mock(st_rdev=123)), patch.object(r.Path, 'glob', return_value=[Path('/proc/100/fd/3')]), patch.object(r.Path, 'iterdir', return_value=iter([])):
            self.assertFalse(r.busy('/dev/nbd3', '101'))

    def test_fatx_device_fd_is_busy(self):
        import stat
        def text(path):
            return 'Tgid:\t100\n' if str(path).endswith('/status') else ''
        with patch.object(r.Path, 'read_text', autospec=True, side_effect=text), patch.object(r.os, 'stat', return_value=Mock(st_rdev=123, st_mode=stat.S_IFBLK)), patch.object(r.Path, 'glob', return_value=[Path('/proc/100/fd/3'), Path('/proc/200/fd/4')]):
            self.assertTrue(r.busy('/dev/nbd3', '101'))

    def test_restore_failure_keeps_state(self):
        self.state.write_text('{}')
        with patch.object(r, 'identity', return_value=None), patch.object(r, 'read', return_value='0'), patch.object(r.Path, 'exists', return_value=True), patch.object(r.subprocess, 'run', return_value=Mock(returncode=1, stderr='ACL failed')):
            with self.assertRaisesRegex(RuntimeError, 'permissions'):
                r.disconnect({'device': '/dev/nbd3', 'acl': self.acl}, self.state)
        self.assertTrue(self.state.exists())


if __name__ == '__main__':
    unittest.main()

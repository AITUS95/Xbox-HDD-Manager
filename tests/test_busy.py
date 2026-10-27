from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import hdd_core as c


class BusyTests(unittest.TestCase):
    def test_busy_process_name(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=0, stdout='123', stderr='')), patch.object(c.Path, 'read_text', return_value='nautilus\n'), patch.object(c.os, 'readlink', side_effect=FileNotFoundError), patch.object(c.Path, 'glob', return_value=[]):
            self.assertEqual(c.mount_users('/test/E'), ['nautilus (PID 123)'])

    def test_working_directory_diagnosed(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=0, stdout='123', stderr='')), patch.object(c.Path, 'read_text', return_value='bash\n'), patch.object(c.os, 'readlink', return_value='/test/E/games'), patch.object(c.Path, 'glob', return_value=[]):
            self.assertIn('working directory', c.mount_users('/test/E')[0])

    def test_deleted_open_file_diagnosed(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=0, stdout='123', stderr='')), patch.object(c.Path, 'read_text', return_value='editor\n'), patch.object(c.os, 'readlink', side_effect=['/home/user', '/test/E/gone.txt (deleted)']), patch.object(c.Path, 'glob', return_value=[Path('/proc/123/fd/4')]):
            self.assertIn('a deleted file is still open', c.mount_users('/test/E')[0])

    def test_no_users(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='')) as run:
            self.assertEqual(c.mount_users('/test/E'), [])
            self.assertEqual(run.call_args.args[0], ['fuser', '-m', '-M', '--', '/test/E'])

    def test_diagnostic_error_not_silenced(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=2, stdout='', stderr='failure')):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                c.mount_users('/test/E')

    def test_process_exit_race(self):
        with patch.object(c.subprocess, 'run', return_value=Mock(returncode=0, stdout='123', stderr='')), patch.object(c.Path, 'read_text', side_effect=FileNotFoundError), patch.object(c.os, 'readlink', side_effect=FileNotFoundError), patch.object(c.Path, 'glob', return_value=[]):
            self.assertEqual(c.mount_users('/test/E'), ['process (PID 123)'])


if __name__ == '__main__':
    unittest.main()

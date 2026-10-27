"""Operations shared by the GUI and tests. No elevated privileges here."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

PARTITIONS = {'C': (0x8ca80000, 0x1f400000), 'E': (0xabe80000, 0x1312d6000),
              'X': (0x80000, 0x2ee00000), 'Y': (0x2ee80000, 0x2ee00000),
              'Z': (0x5dc80000, 0x2ee00000)}


def run(args, **kwargs):
    p = subprocess.run([str(a) for a in args], text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, close_fds=True, **kwargs)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f'Command failed: {args[0]} ({p.returncode})')
    return p.stdout


def image_info(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise RuntimeError('Select a QCOW2 or RAW image file, not a system disk.')
    d = json.loads(run(['qemu-img', 'info', '--output=json', str(path)]))
    if d.get('format') not in ('qcow2', 'raw') or d.get('backing-filename') or d.get('encrypted'):
        raise RuntimeError('Only standalone, unencrypted QCOW2 or RAW images are supported.')
    if d.get('format-specific', {}).get('data', {}).get('data-file'):
        raise RuntimeError('Images with an external data file are not supported.')
    return d


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def backup(image, directory):
    """Call only with NBD's exclusive QEMU writer lock held and no FATX mounts."""
    image, directory = Path(image), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    before = image.stat()
    fd, temporary = tempfile.mkstemp(prefix=image.stem + '-', suffix='.partial', dir=directory)
    os.close(fd)
    temporary = Path(temporary)
    try:
        run(['cp', '--reflink=auto', '--sparse=always', '--', image, temporary])
        if digest(image) != digest(temporary):
            raise RuntimeError('Backup SHA-256 verification failed.')
        after = image.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError('The image changed while the backup was being created.')
        with temporary.open('rb') as f:
            os.fsync(f.fileno())
        destination = temporary.with_suffix(image.suffix or '.img')
        temporary.rename(destination)
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return destination
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def probe(device, size):
    result = {}
    with open(device, 'rb', buffering=0) as f:
        for letter, (offset, length) in PARTITIONS.items():
            if offset + length > size:
                continue
            f.seek(offset)
            if f.read(4) == b'FATX':
                result[letter] = (offset, length)
    return result


def mount_records():
    def unescape(s):
        for a, b in [('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')]:
            s = s.replace(a, b)
        return s
    records = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        records[unescape(fields[4])] = (fs[0], unescape(fs[1]))
    return records


def mount_users(path):
    """Diagnose users of this exact mount; never signal or terminate them."""
    p = subprocess.run(['fuser', '-m', '-M', '--', str(path)], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       close_fds=True, timeout=5)
    if p.returncode not in (0, 1):
        raise RuntimeError('Partition diagnostics failed: ' + p.stderr.strip())
    users = []
    for token in p.stdout.split():
        if not token.isdigit():
            continue
        proc = Path('/proc') / token
        try:
            name = (proc / 'comm').read_text().strip()
        except OSError:
            name = 'process'
        details = []
        try:
            cwd = os.readlink(proc / 'cwd')
            if cwd == str(path) or cwd.startswith(str(path) + '/'):
                details.append('working directory is on the partition: change directories or close the terminal')
        except OSError:
            pass
        for fd in (proc / 'fd').glob('*'):
            try:
                target = os.readlink(fd)
                if (target == str(path) or target.startswith(str(path) + '/')) and target.endswith(' (deleted)'):
                    details.append('a deleted file is still open: close the program using it')
                    break
            except OSError:
                pass
        users.append(f"{name} (PID {token})" + (': ' + '; '.join(details) if details else ''))
    return users


def busy_message(details):
    return ('Unmount postponed: a program is still using the partition.\n'
            + '\n'.join(details) +
            '\nClose file manager windows, editors, and terminals open on the partitions; '
            'wait for copies or deletions to finish, then retry Unmount and disconnect. '
            'The disk remains connected to protect pending writes.')


class BusyPartitions(RuntimeError):
    """A recoverable condition: keep the connection and let the user retry."""
    def __init__(self, details):
        super().__init__(busy_message(details))
        self.details = details
        self.nautilus = any('nautilus (PID ' in detail for detail in details)


def layout_paths(base):
    """Allow updates to preserve an existing installation's user-data folders."""
    base = Path(base)
    try:
        layout = json.loads((base / 'layout.json').read_text())
    except FileNotFoundError:
        layout = {}
    if not isinstance(layout, dict):
        raise RuntimeError('Invalid application directory layout.')
    result = []
    for key, default in [('mounts', 'Mounts'), ('backups', 'Backups'), ('images', 'images.json')]:
        value = layout.get(key, default)
        if not isinstance(value, str) or not value or value in ('.', '..') or Path(value).name != value:
            raise RuntimeError(f'Invalid directory layout: {key}.')
        result.append(base / value)
    return result


class Manager:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.mounts, self.backups, self.images_file = layout_paths(self.base)
        self.logs = self.base / 'logs'
        self.logs.mkdir(parents=True, exist_ok=True)
        self.lock = (self.base / 'manager.lock').open('a')
        # Python descriptors are CLOEXEC; subprocesses always use close_fds=True.
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Xbox HDD Manager is already running.')
        self.uid = os.getuid()
        self.statefile = Path(f'/run/xbox-hdd-manager/{self.uid}.json')

    def state(self):
        try:
            return json.loads(self.statefile.read_text())
        except FileNotFoundError:
            return None

    def helper(self, action, *args):
        output = run(['pkexec', '/usr/bin/python3', '-I', self.base / 'root_helper.py', action, *args])
        return json.loads(output)

    def mounted(self):
        return {p: value for p, value in mount_records().items()
                if str(Path(p).parent) == str(self.mounts)}

    def check_mountpoints(self):
        if self.mounted():
            raise RuntimeError('Partitions are mounted. Unmount them first.')
        for letter in PARTITIONS:
            path = self.mounts / letter
            if path.is_symlink():
                raise RuntimeError(f'Symbolic mount point not allowed: {path}')
            path.mkdir(parents=True, exist_ok=True)
            if any(path.iterdir()):
                raise RuntimeError(f'The directory {path} is not empty; its files will not be hidden.')

    def connect(self, image, readonly):
        if self.state():
            raise RuntimeError('A previous session exists. Use Unmount and disconnect.')
        self.check_mountpoints()
        image = Path(image).resolve(strict=True)
        image_info(image)
        return self.helper('connect', str(image), 'ro' if readonly else 'rw')

    def mount(self, image, readonly=True):
        if not Path('/dev/fuse').exists():
            raise RuntimeError('/dev/fuse is unavailable: run on a Linux host with FUSE enabled.')
        driver = self.base / 'bin/fatxfs'
        if not driver.is_file():
            raise RuntimeError('FATX driver missing: run install.sh.')
        state = self.connect(image, readonly)
        try:
            backup_path = None
            if not readonly:
                backup_path = backup(state['image'], self.backups)
            available = probe(state['device'], state['size'])
            if not available:
                raise RuntimeError('No valid retail FATX partitions found. Expanded F/G partitions are not supported.')
            for letter in available:
                path = self.mounts / letter
                logpath = self.logs / f'{letter}.log'
                # Single threaded: libfatx shares a FILE cursor and a mutable FAT cache.
                # Paths are absolute: no cwd dependence when started from a launcher.
                with logpath.open('w') as log:
                    p = subprocess.run([str(driver), state['device'], str(path),
                                        f'--drive={letter.lower()}', '-s', '-o',
                                        ('ro' if readonly else 'rw') + ',fsname=' + state['device']], stdout=log, stderr=log,
                                       close_fds=True, timeout=30)
                if p.returncode or str(path) not in self.mounted():
                    raise RuntimeError(f'Mounting {letter} failed. Details: {logpath}')
            return f"Partitions mounted: {', '.join(available)}" + (f'\nVerified backup: {backup_path}' if backup_path else '\nRead-only mode.')
        except BaseException as error:
            try:
                self.unmount()
            except Exception as cleanup:
                raise RuntimeError(f'{error}\nCleanup incomplete: {cleanup}\nRetry Unmount and disconnect.') from error
            raise

    def unmount(self):
        state = self.state()
        mounted = self.mounted()
        if mounted and not state:
            raise RuntimeError('Mounted partitions have no matching session state; they will not be changed.')
        # Check every managed mount before changing any of them. A busy E must
        # not leave C/X/Y/Z unexpectedly unmounted. Kernel checks still handle races.
        blockers = []
        for path, (kind, source) in mounted.items():
            if not kind.startswith('fuse') or source != state['device']:
                raise RuntimeError(f'Unrecognized mount: {path}. No partitions were unmounted.')
            users = mount_users(path)
            if users:
                blockers.append(f"{Path(path).name}: " + ', '.join(users))
        if blockers:
            raise BusyPartitions(blockers)
        failures = []
        for path, (kind, source) in mounted.items():
            if not kind.startswith('fuse') or source != state['device']:
                failures.append(f'Unrecognized mount: {path}')
                continue
            try:
                run(['fusermount', '-u', path], timeout=30)
            except Exception as exc:
                try:
                    users = mount_users(path)
                except Exception:
                    users = []
                failures.append(f"{Path(path).name}: " + ', '.join(users) + '\n' + str(exc))
        if failures or self.mounted():
            raise BusyPartitions(failures)
        if state:
            self.helper('disconnect')
        return 'Partitions unmounted and disk disconnected.'

    def close_files_and_unmount(self):
        """Only called by the explicit 'Close Files and retry' UI action.

        --quit is Nautilus' normal quit request, with no signal or forced unmount.
        The user confirms all file operations have finished in the busy dialog.
        Kernel checks and all ownership checks remain in unmount().
        """
        state = self.state()
        if not state:
            return self.unmount()
        users = []
        for path, (kind, source) in self.mounted().items():
            if not kind.startswith('fuse') or source != state['device']:
                raise RuntimeError('Unrecognized mount: chiusura di File annullata.')
            users.extend(mount_users(path))
        if any(user.startswith('nautilus (PID ') for user in users):
            run(['nautilus', '--quit'], timeout=15)
        # Nautilus may release its descriptors asynchronously after --quit returns.
        for attempt in range(10):
            try:
                return self.unmount()
            except BusyPartitions:
                if attempt == 9:
                    raise
                time.sleep(.2)

    def backup_only(self, image):
        self.connect(image, True)
        try:
            result = backup(image, self.backups)
        finally:
            self.unmount()
        return f'Verified backup: {result}'

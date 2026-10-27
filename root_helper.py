#!/usr/bin/python3
"""Invoked explicitly through pkexec; no permanent privileged executable/policy."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

# Do not import user modules in a privileged process. Run with python -I.
os.environ['PATH'] = '/usr/sbin:/usr/bin:/sbin:/bin'
RUNTIME = Path('/run/xbox-hdd-manager')
SYS = Path('/sys/block')


def run(args):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       close_fds=True, timeout=45)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f'Command failed: {args[0]}')
    return p.stdout


def read(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ''


def identity(device):
    node = SYS / Path(device).name
    pid = read(node / 'pid')
    if not pid.isdigit():
        return None
    try:
        # PID + kernel start time protects against PID reuse.
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        return None
    return {'pid': pid, 'start': start}


def save(path, state):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    temporary.chmod(0o644)
    temporary.replace(path)


def free_devices():
    for node in sorted(SYS.glob('nbd*'), key=lambda x: int(x.name[3:])):
        device = Path('/dev') / node.name
        if (not read(node / 'pid') and read(node / 'size') == '0'
                and device.exists() and stat.S_ISBLK(device.stat().st_mode)):
            yield str(device)


def busy(device, owner_pid):
    """FATX uses an open device fd; also reject kernel mounts of NBD/partitions."""
    # QEMU retains the device fd in its kernel-client thread.
    # Exempt only the verified connection's thread group, never FATX.
    status = Path(f'/proc/{owner_pid}/status').read_text()
    owner_tgid = next(line.split()[1] for line in status.splitlines() if line.startswith('Tgid:'))
    number = os.stat(device).st_rdev
    node = Path(device).name
    for mount in Path('/proc/self/mountinfo').read_text().splitlines():
        source = mount.split(' - ', 1)[1].split()[1]
        if source == device or re.fullmatch(re.escape(device) + r'p\d+', source):
            return True
    for fd in Path('/proc').glob('[0-9]*/fd/*'):
        if fd.parts[2] == owner_tgid:
            continue
        try:
            s = fd.stat()
            if stat.S_ISBLK(s.st_mode) and (s.st_rdev == number or
                    re.fullmatch(re.escape(device) + r'p\d+', os.readlink(fd))):
                return True
        except FileNotFoundError:
            pass
        # Permission errors intentionally abort rather than declaring it safe.
    return bool(list((SYS / node / 'holders').iterdir()))


def disconnect(state, statefile):
    device = state['device']
    current = identity(device)
    if current is not None and current != state.get('identity'):
        raise RuntimeError('NBD identity changed; refusing to disconnect another program’s disk.')
    if current:
        if busy(device, current['pid']):
            raise RuntimeError('The disk is still open or mounted. Close programs and unmount first.')
        run(['blockdev', '--flushbufs', device])
        run(['qemu-nbd', '--disconnect', device])
        for _ in range(100):
            if identity(device) is None and read(SYS / Path(device).name / 'size') == '0':
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Disconnection is not complete. Try again.')
    elif read(SYS / Path(device).name / 'size') not in ('', '0'):
        raise RuntimeError('Ambiguous NBD state: refusing automatic disconnection.')
    if Path(device).exists():
        restored = subprocess.run(['setfacl', '--set-file=-', device], input=state['acl'],
                                  text=True, capture_output=True, close_fds=True, timeout=15)
        if restored.returncode:
            raise RuntimeError('Failed to restore NBD permissions: ' + restored.stderr)
    statefile.unlink(missing_ok=True)


def connect(image, mode, uid, statefile):
    image = Path(image).resolve(strict=True)
    if not image.is_file() or image.stat().st_uid != uid:
        raise RuntimeError('Select an image file owned by your user.')
    if mode not in ('ro', 'rw'):
        raise RuntimeError('Invalid mode.')
    if statefile.exists():
        raise RuntimeError('A session is already registered. Unmount it first.')
    d = json.loads(run(['qemu-img', 'info', '--output=json', str(image)]))
    if d.get('format') not in ('qcow2', 'raw') or d.get('backing-filename') or d.get('encrypted') or d.get('format-specific', {}).get('data', {}).get('data-file'):
        raise RuntimeError('Unsupported format: a standalone QCOW2 or RAW image is required.')
    usage = subprocess.run(['fuser', '-s', '--', str(image)], close_fds=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
    if usage.returncode == 0:
        raise RuntimeError('Image is in use. Close xemu and other programs first.')
    if usage.returncode != 1:
        raise RuntimeError('Unable to check whether the image is in use.')
    run(['modprobe', 'nbd', 'max_part=8'])
    device = next(free_devices(), None)
    if not device:
        raise RuntimeError('No free NBD device available.')
    # QEMU writer lock is held even for read-only FUSE sessions: no --shared/-U.
    # The underlying NBD is writable because libfatx fopen requires r+b.
    # Read-only is enforced by FUSE, while the writer lock protects all backups.
    acl = run(['getfacl', '-cp', device])
    state = {'device': device, 'image': str(image), 'size': d['virtual-size'],
             'readonly': mode == 'ro', 'identity': None, 'acl': acl}
    run(['qemu-nbd', '--format=' + d['format'], '--cache=writeback', '--connect=' + device, str(image)])
    try:
        for _ in range(150):
            ident = identity(device)
            if ident:
                state['identity'] = ident
                save(statefile, state)
            if ident and int(read(SYS / Path(device).name / 'size') or '0') * 512 == state['size']:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('NBD did not become ready within 15 seconds.')
        run(['setfacl', '-m', f'u:{uid}:rw', device])
        return state
    except BaseException:
        # Preserve state if disconnect fails; never silently abandon a connection.
        if state['identity']:
            save(statefile, state)
            disconnect(state, statefile)
        else:
            run(['qemu-nbd', '--disconnect', device])
        raise


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run through pkexec.')
    os.umask(0o022)
    uid = int(os.environ.get('PKEXEC_UID', '-1'))
    if uid <= 0:
        raise RuntimeError('Invalid pkexec user.')
    RUNTIME.mkdir(mode=0o755, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0 or RUNTIME.stat().st_mode & 0o022:
        raise RuntimeError('Unsafe runtime state directory.')
    # Serialize all users: free-device selection is one transaction.
    with (RUNTIME / 'global.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        statefile = RUNTIME / f'{uid}.json'
        args = sys.argv[1:]
        if len(args) == 3 and args[0] == 'connect':
            result = connect(args[1], args[2], uid, statefile)
        elif args == ['disconnect']:
            if statefile.exists():
                disconnect(json.loads(statefile.read_text()), statefile)
            result = {'disconnected': True}
        else:
            raise RuntimeError('Operation not permitted.')
        print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

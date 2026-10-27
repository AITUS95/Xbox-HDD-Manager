#!/usr/bin/python3
"""Update a user-selected installation while preserving its data directories."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from hdd_core import layout_paths, mount_records

FILES = ['hdd_core.py', 'root_helper.py', 'xbox_hdd_manager.py', 'xbox-hdd-manager.sh',
         'install.sh', 'uninstall.sh', 'update.py', 'update.sh', 'patch_driver.py',
         'icon.png', 'README.md', 'LICENSE']


def update(source, target, layout):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not (target / 'bin/fatxfs').is_file():
        raise RuntimeError('Select an existing installation and run the update from the extracted source package.')
    for name in FILES:
        if not (source / name).is_file():
            raise RuntimeError(f'Missing source file: {name}')
    # Accept only simple names beneath the installation directory.
    for key in ('mounts', 'backups', 'images'):
        value = layout[key]
        if not value or value in ('.', '..') or Path(value).name != value:
            raise RuntimeError(f'Invalid layout name for {key}.')
    with (target / 'manager.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Close Xbox HDD Manager before updating.')
        if Path(f'/run/xbox-hdd-manager/{os.getuid()}.json').exists():
            raise RuntimeError('Unmount and disconnect the active image before updating.')
        mount_roots = {target / layout['mounts'], layout_paths(target)[0]}
        if any(any(Path(p) == root or root in Path(p).parents for root in mount_roots)
               for p in mount_records()):
            raise RuntimeError('Unmount the partitions before updating.')
        # Build before changing the installation. Existing backups are never copied or moved.
        with tempfile.TemporaryDirectory(prefix='xbox-hdd-build-') as work:
            work = Path(work)
            prefix = work / 'prefix'
            commands = [
                ['cmake', '-S', source / 'driver/libfatx', '-B', work / 'lib', f'-DCMAKE_INSTALL_PREFIX={prefix}', '-DCMAKE_BUILD_TYPE=Release'],
                ['cmake', '--build', work / 'lib', '-j', str(os.cpu_count() or 1)],
                ['cmake', '--install', work / 'lib'],
                ['cmake', '-S', source / 'driver/fatxfs', '-B', work / 'fuse', f'-DCMAKE_PREFIX_PATH={prefix}', f'-DCMAKE_INSTALL_PREFIX={prefix}', '-DCMAKE_BUILD_TYPE=Release'],
                ['cmake', '--build', work / 'fuse', '-j', str(os.cpu_count() or 1)],
                ['cmake', '--install', work / 'fuse'],
            ]
            for command in commands:
                subprocess.run([str(a) for a in command], check=True)
            binary = prefix / 'bin/fatxfs'
            if not binary.is_file():
                raise RuntimeError('FATX driver build failed.')
            # Archive only application files, never image data or backups.
            replacements = {name: source / name for name in FILES}
            replacements['bin/fatxfs'] = binary
            layout_source = work / 'layout.json'
            layout_source.write_text(json.dumps(layout, indent=2) + '\n')
            replacements['layout.json'] = layout_source
            archive = target / 'Previous' / ('update-' + str(time.time_ns()))
            archive.mkdir(parents=True)
            for name in replacements:
                old = target / name
                if old.is_file():
                    (archive / name).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old, archive / name)
            completed = []
            try:
                for name, src in replacements.items():
                    dest = target / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    fd, temp = tempfile.mkstemp(prefix='.update-', dir=dest.parent)
                    tmp = Path(temp)
                    try:
                        with os.fdopen(fd, 'wb') as output, src.open('rb') as inp:
                            shutil.copyfileobj(inp, output)
                            output.flush()
                            os.fsync(output.fileno())
                        tmp.chmod(0o755 if name.endswith('.sh') or name == 'bin/fatxfs' else 0o644)
                        tmp.replace(dest)
                        completed.append(name)
                    finally:
                        tmp.unlink(missing_ok=True)
            except BaseException:
                for name in reversed(completed):
                    dest, old = target / name, archive / name
                    if old.exists():
                        shutil.copy2(old, dest)
                    else:
                        dest.unlink(missing_ok=True)
                raise
    print('Update complete. Reopen Xbox HDD Manager from the application menu.')
    print('The existing image list, mount folders, and backups were preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='Existing application installation directory')
    parser.add_argument('--mounts-folder', default='Mounts', help='Existing mount directory name within the installation')
    parser.add_argument('--backups-folder', default='Backups', help='Existing backup directory name within the installation')
    parser.add_argument('--image-list', default='images.json', help='Existing image-list file name within the installation')
    args = parser.parse_args()
    if os.getuid() == 0:
        raise RuntimeError('Run this updater as a normal user, not with sudo.')
    update(Path(__file__).parent, args.target,
           {'mounts': args.mounts_folder, 'backups': args.backups_folder, 'images': args.image_list})


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import sys
        print(str(exc), file=sys.stderr)
        sys.exit(1)

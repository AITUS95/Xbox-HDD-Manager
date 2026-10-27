#!/usr/bin/python3
"""GTK interface; all long operations run outside the GTK event loop."""
import json
from pathlib import Path
import subprocess
import sys
import threading

from hdd_core import Manager, BusyPartitions


def main():
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib
    base = Path(__file__).resolve().parent
    try:
        manager = Manager(base)
    except Exception as exc:
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=str(exc))
        dialog.run()
        dialog.destroy()
        return 1

    class Window(Gtk.Window):
        def __init__(self):
            super().__init__(title='Xbox HDD Manager')
            self.set_default_size(760, 340)
            self.set_border_width(20)
            self.working = False
            self.connect('delete-event', self.closing)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            self.add(box)
            label = Gtk.Label(label='Xbox disk image (QCOW2 or RAW)', xalign=0)
            box.pack_start(label, False, False, 0)
            row = Gtk.Box(spacing=8)
            box.pack_start(row, False, False, 0)
            self.combo = Gtk.ComboBoxText()
            self.combo.set_hexpand(True)
            row.pack_start(self.combo, True, True, 0)
            browse = Gtk.Button(label='Add image…')
            browse.connect('clicked', self.browse)
            row.pack_start(browse, False, False, 0)
            self.paths = []
            try:
                stored = json.loads(manager.images_file.read_text())
                if not isinstance(stored, list):
                    stored = []
            except (OSError, ValueError):
                stored = []
            candidates = stored[:]
            state = manager.state()
            if state:
                candidates.insert(0, state['image'])
            for p in candidates:
                if isinstance(p, str) and p not in self.paths:
                    self.add_path(p)
            if self.paths:
                self.combo.set_active(0)
            self.readonly = Gtk.CheckButton(label='Read-only (disable to edit; an automatic backup is required)')
            self.readonly.set_active(True)
            box.pack_start(self.readonly, False, False, 0)
            self.status = Gtk.Label(xalign=0)
            self.status.set_line_wrap(True)
            self.status.set_selectable(True)
            box.pack_start(self.status, True, True, 0)
            actions = Gtk.Box(spacing=8)
            box.pack_start(actions, False, False, 0)
            self.controls = [self.combo, browse, self.readonly]
            for title, callback in [('Mount', self.mount), ('Unmount and disconnect', self.unmount),
                                    ('Create backup', self.backup), ('Open folder', self.open_folder)]:
                button = Gtk.Button(label=title)
                button.connect('clicked', callback)
                actions.pack_start(button, True, True, 0)
                self.controls.append(button)
            self.refresh()

        def add_path(self, path):
            self.paths.append(path)
            self.combo.append_text(path)

        def selected(self):
            i = self.combo.get_active()
            if i < 0:
                raise RuntimeError('Add and select a disk image.')
            return self.paths[i]

        def refresh(self):
            state = manager.state()
            mounted = manager.mounted()
            self.combo.set_sensitive(not state and not self.working)
            self.readonly.set_sensitive(not state and not self.working)
            if state:
                self.status.set_text(f"Connected disk: {state['image']}\n{state['device']} • " +
                                     ('Read-only' if state['readonly'] else 'Read and write') +
                                     f" • Partitions mounted: {len(mounted)}\nWhen finished, choose Unmount and disconnect.")
            else:
                self.status.set_text('Disk disconnected. Close xemu before mounting.\n' +
                                     f'Mounts and backups: {base}')

        def message(self, text, error=False):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.CLOSE, text=text)
            dialog.run()
            dialog.destroy()

        def perform(self, function):
            if self.working:
                return
            self.working = True
            for control in self.controls:
                control.set_sensitive(False)
            self.status.set_text('Operation in progress… Backups can take several minutes.')
            def worker():
                try:
                    result, error = function(), False
                except Exception as exc:
                    result, error = exc, True
                    with (base / 'logs/errors.log').open('a') as log:
                        import traceback
                        traceback.print_exc(file=log)
                GLib.idle_add(self.finished, result, error)
            threading.Thread(target=worker, daemon=False).start()

        def finished(self, result, error):
            self.working = False
            for control in self.controls:
                control.set_sensitive(True)
            self.refresh()
            if isinstance(result, BusyPartitions):
                self.busy_dialog(result)
            else:
                self.message(str(result), error)
            return False

        def busy_dialog(self, error):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING, text=str(error))
            dialog.add_button('Leave connected', Gtk.ResponseType.CANCEL)
            dialog.add_button('Retry', 1)
            if error.nautilus:
                dialog.format_secondary_text(
                    'Nautilus (Files) is holding the partition open. When copies and deletions '
                    'are complete, you can close all Files windows and retry. '
                    'Other programs remain open.')
                dialog.add_button('Copies are complete: close Files and retry', 2)
            response = dialog.run()
            dialog.destroy()
            if response == 1:
                self.perform(manager.unmount)
            elif response == 2:
                self.perform(manager.close_files_and_unmount)

        def browse(self, _):
            if manager.state():
                self.message('Unmount the connected disk before changing it.', True)
                return
            dialog = Gtk.FileChooserDialog(title='Select Xbox HDD image', transient_for=self,
                                           action=Gtk.FileChooserAction.OPEN)
            dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Select', Gtk.ResponseType.OK)
            if dialog.run() == Gtk.ResponseType.OK:
                path = dialog.get_filename()
                if path not in self.paths:
                    self.add_path(path)
                    temp = manager.images_file.with_name(manager.images_file.name + '.tmp')
                    temp.write_text(json.dumps(self.paths, ensure_ascii=False))
                    temp.replace(manager.images_file)
                self.combo.set_active(self.paths.index(path))
            dialog.destroy()

        def mount(self, _):
            try:
                image = self.selected()
            except Exception as exc:
                self.message(str(exc), True)
                return
            ro = self.readonly.get_active()
            if not ro:
                dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                    message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                    text='Mount read/write? Close xemu. A verified backup will be made before any changes. Do not start xemu while the disk is connected.')
                answer = dialog.run()
                dialog.destroy()
                if answer != Gtk.ResponseType.YES:
                    return
            self.perform(lambda: manager.mount(image, ro))

        def unmount(self, _):
            self.perform(manager.unmount)

        def backup(self, _):
            try:
                image = self.selected()
            except Exception as exc:
                self.message(str(exc), True)
                return
            self.perform(lambda: manager.backup_only(image))

        def open_folder(self, _):
            manager.mounts.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(['xdg-open', str(manager.mounts)], close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def closing(self, *_):
            if self.working:
                return True
            if manager.state() or manager.mounted():
                self.message('The disk is still connected. Choose Unmount and disconnect before closing.', True)
                return True
            Gtk.main_quit()
            return False

    window = Window()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == '__main__':
    sys.exit(main())

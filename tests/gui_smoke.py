"""Launch under xvfb; capture the actual GTK window and exit automatically."""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk
import xbox_hdd_manager


def inspect():
    windows = [w for w in Gtk.Window.list_toplevels() if w.get_visible()]
    assert windows, 'No window'
    window = windows[0]
    def all_widgets(widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from all_widgets(child)
    widgets = list(all_widgets(window))
    combos = [w for w in widgets if isinstance(w, Gtk.ComboBoxText)]
    assert len(combos) == 1
    assert combos[0].get_active_text() is None
    toggles = [w for w in widgets if isinstance(w, Gtk.CheckButton)]
    assert toggles[0].get_active()
    buttons = [w.get_label() for w in widgets if isinstance(w, Gtk.Button)]
    for title in ['Mount', 'Unmount and disconnect', 'Create backup', 'Open folder']:
        assert title in buttons
    width, height = window.get_size()
    pixbuf = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0, width, height)
    pixbuf.savev('/tmp/xbox-gui.png', 'png', [], [])
    print('GTK smoke OK: dropdown, no preselected image, read-only default, all action buttons')
    Gtk.main_quit()
    return False

GLib.timeout_add(700, inspect)
xbox_hdd_manager.main()

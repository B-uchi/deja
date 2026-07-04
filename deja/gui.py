"""GTK4 quick picker.

Single click selects; double-click or Enter copies and closes. Selection
mode (Ctrl+S or the header toggle) shows checkboxes for bulk pin/delete.

Copying goes through clip.copy_text, not this window's own GDK clipboard:
a Wayland offer dies with its client, and this window closes right after
copying — the long-lived daemon must own it.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:  # libadwaita: native GNOME stylesheet + dark mode; optional
    gi.require_version("Adw", "1")
    from gi.repository import Adw  # noqa: E402
    Adw.init()
except (ImportError, ValueError):
    Adw = None

from . import APP_ID, clip, util  # noqa: E402
from .store import Store  # noqa: E402

CSS = b"""
.deja-list { background: alpha(currentColor, 0.045); border-radius: 12px; }
.deja-list > row { padding: 10px 12px; }
.deja-list > row:not(:last-child) {
    border-bottom: 1px solid alpha(currentColor, 0.08);
}
.deja-preview { font-family: monospace; font-size: 10.5pt; }
.deja-age    { font-size: 8pt; opacity: 0.65; }
.deja-times  { font-size: 8pt; opacity: 0.45; }
.deja-pin    { color: #e5a50a; font-size: 10pt; }
.deja-hint   { font-size: 8pt; opacity: 0.5; }
.deja-empty-title { font-size: 13pt; font-weight: bold; opacity: 0.65; }
.deja-empty-sub   { font-size: 9.5pt; opacity: 0.45; }
.deja-toast {
    background: alpha(#26a269, 0.95); color: white;
    border-radius: 99px; padding: 8px 18px; font-weight: 600;
}
"""


class DejaWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="deja")
        self.store = Store()
        self.select_mode = False
        self.checked: set[int] = set()
        self._pinned = {}              # entry id -> pinned (from last refresh)
        self._toast_timer = None
        self.set_default_size(640, 520)

        self._build_header()
        self._build_body()
        self._build_css()

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.search.set_key_capture_widget(self)   # type anywhere to search

        self.refresh()
        self.search.grab_focus()

    # ------------------------------------------------------------ structure

    def _build_header(self):
        header = Gtk.HeaderBar()
        self.search = Gtk.SearchEntry(
            placeholder_text="Search your clipboard history", hexpand=True,
            width_chars=34)
        self.search.connect("search-changed", lambda *_: self.refresh())
        self.search.connect("activate", lambda *_: self.copy_selected())
        header.set_title_widget(self.search)

        self.select_btn = Gtk.ToggleButton(icon_name="object-select-symbolic",
                                           tooltip_text="Select (Ctrl+S)")
        self.select_btn.connect("toggled", self._on_select_toggled)
        header.pack_end(self.select_btn)
        self.set_titlebar(header)

    def _build_body(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        overlay = Gtk.Overlay(child=root)
        self.set_child(overlay)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("deja-list")
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-activated", self._on_row_activated)
        self.listbox.set_placeholder(self._build_placeholder())

        scroll = Gtk.ScrolledWindow(
            vexpand=True, child=self.listbox,
            margin_top=12, margin_bottom=6, margin_start=12, margin_end=12)
        root.append(scroll)

        # footer: hints normally, action bar in select mode
        self.footer = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE)
        hint = Gtk.Label(label="Enter or double-click copies  ·  Ctrl+P pin  ·  "
                               "Del delete  ·  Ctrl+S select  ·  Esc close")
        hint.add_css_class("deja-hint")
        hint.props.margin_bottom = 8
        self.footer.add_named(hint, "hints")

        bar = Gtk.ActionBar()
        self.count_label = Gtk.Label(label="0 selected")
        self.count_label.add_css_class("dim-label")
        bar.pack_start(self.count_label)
        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", lambda *_: self._apply_delete())
        bar.pack_end(self.delete_btn)
        self.pin_btn = Gtk.Button(label="Pin")
        self.pin_btn.connect("clicked", lambda *_: self._apply_pin())
        bar.pack_end(self.pin_btn)
        self.footer.add_named(bar, "actions")
        root.append(self.footer)

        self.toast = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=18)
        self.toast_label = Gtk.Label(label="")
        self.toast_label.add_css_class("deja-toast")
        self.toast.set_child(self.toast_label)
        overlay.add_overlay(self.toast)

    def _build_placeholder(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      valign=Gtk.Align.CENTER, margin_top=48, margin_bottom=48)
        icon = Gtk.Image.new_from_icon_name("edit-paste-symbolic")
        icon.set_pixel_size(48)
        icon.set_opacity(0.3)
        box.append(icon)
        title = Gtk.Label(label="Nothing here yet")
        title.add_css_class("deja-empty-title")
        box.append(title)
        sub = Gtk.Label(label="Copy something and it appears instantly.\n"
                              "Daemon asleep? Check `deja status`.")
        sub.set_justify(Gtk.Justification.CENTER)
        sub.add_css_class("deja-empty-sub")
        box.append(sub)
        return box

    def _build_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ----------------------------------------------------------------- data

    def refresh(self):
        query = self.search.get_text().strip()
        rows = (self.store.search(query, limit=60) if query
                else self.store.recent(limit=60))
        self._pinned = {r["id"]: bool(r["pinned"]) for r in rows}
        self.listbox.remove_all()
        for r in rows:
            self.listbox.append(self._make_row(r))
        first = self.listbox.get_row_at_index(0)
        if first:
            self.listbox.select_row(first)
        self._sync_action_state()

    def _make_row(self, r) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.entry_id = r["id"]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        check = Gtk.CheckButton(active=r["id"] in self.checked)
        check.connect("toggled", self._on_check_toggled, r["id"])
        row.check_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
            reveal_child=self.select_mode, child=check)
        box.append(row.check_revealer)

        if r["pinned"]:
            pin = Gtk.Label(label="★", valign=Gtk.Align.CENTER)
            pin.add_css_class("deja-pin")
            box.append(pin)

        text = Gtk.Label(label=util.preview(r["content"], 200),
                         xalign=0, hexpand=True,
                         ellipsize=Pango.EllipsizeMode.END)
        text.add_css_class("deja-preview")
        box.append(text)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       valign=Gtk.Align.CENTER)
        age = Gtk.Label(label=util.fmt_age(r["last_seen"]), xalign=1)
        age.add_css_class("deja-age")
        meta.append(age)
        if r["times_seen"] > 1:
            times = Gtk.Label(label=f"{r['times_seen']}×", xalign=1)
            times.add_css_class("deja-times")
            meta.append(times)
        box.append(meta)

        row.set_child(box)
        return row

    # ------------------------------------------------------- selection mode

    def _on_select_toggled(self, btn):
        self.set_select_mode(btn.get_active())

    def set_select_mode(self, on: bool):
        if on == self.select_mode:
            return
        self.select_mode = on
        self.select_btn.set_active(on)
        self.listbox.set_activate_on_single_click(on)
        self.footer.set_visible_child_name("actions" if on else "hints")
        row = self.listbox.get_row_at_index(0)
        i = 0
        while row is not None:
            row.check_revealer.set_reveal_child(on)
            i += 1
            row = self.listbox.get_row_at_index(i)
        if not on:
            self.checked.clear()
        self._sync_action_state()

    def _on_check_toggled(self, check, entry_id):
        (self.checked.add if check.get_active()
         else self.checked.discard)(entry_id)
        self._sync_action_state()

    def _sync_action_state(self):
        self.checked &= set(self._pinned)      # drop ids no longer listed
        n = len(self.checked)
        self.count_label.set_label(f"{n} selected")
        self.pin_btn.set_sensitive(n > 0)
        self.delete_btn.set_sensitive(n > 0)
        all_pinned = n > 0 and all(self._pinned.get(i) for i in self.checked)
        self.pin_btn.set_label("Unpin" if all_pinned else "Pin")

    def _check_all(self):
        self.checked = set(self._pinned)
        self.refresh()

    def _apply_pin(self):
        ids = sorted(self.checked)
        if not ids:
            return
        unpin = all(self._pinned.get(i) for i in ids)
        for i in ids:
            self.store.set_pinned(i, not unpin)
        self.show_toast(f"{len(ids)} {'unpinned' if unpin else 'pinned'}")
        self.refresh()

    def _apply_delete(self):
        ids = sorted(self.checked)
        if not ids:
            return
        for i in ids:
            self.store.delete(i)
        self.checked.clear()
        self.show_toast(f"{len(ids)} deleted")
        self.refresh()

    # -------------------------------------------------------------- actions

    def selected(self) -> Gtk.ListBoxRow | None:
        return (self.listbox.get_selected_row()
                or self.listbox.get_row_at_index(0))

    def copy_selected(self):
        if self.select_mode:
            return
        row = self.selected()
        if row:
            self.copy_row(row)

    def _on_row_activated(self, _lb, row):
        if self.select_mode:
            check = row.check_revealer.get_child()
            check.set_active(not check.get_active())
        else:
            self.copy_row(row)

    def copy_row(self, row):
        entry = self.store.get(row.entry_id)
        if entry and clip.copy_text(entry["content"]):
            self.show_toast("Copied — ready to paste", hold=True)
            GLib.timeout_add(420, self.close)
        else:
            self.show_toast("Clipboard unavailable — is the daemon running?")

    def show_toast(self, msg: str, hold: bool = False):
        self.toast_label.set_label(msg)
        self.toast.set_reveal_child(True)
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = None
        if not hold:
            self._toast_timer = GLib.timeout_add(
                1300, lambda: (self.toast.set_reveal_child(False), False)[1])

    # ------------------------------------------------------------- keyboard

    def on_key(self, _ctrl, keyval, _keycode, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_Escape:
            if self.select_mode:
                self.set_select_mode(False)
            else:
                self.close()
            return True
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.set_select_mode(not self.select_mode)
            return True
        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A) and self.select_mode:
            self._check_all()
            return True
        if keyval == Gdk.KEY_Down and self.search.has_focus():
            row = self.selected()
            if row:
                self.listbox.grab_focus()
                self.listbox.select_row(row)
            return True
        if ctrl and keyval in (Gdk.KEY_p, Gdk.KEY_P):
            if self.select_mode:
                self._apply_pin()
            else:
                row = self.selected()
                if row:
                    entry = self.store.get(row.entry_id)
                    self.store.set_pinned(row.entry_id, not entry["pinned"])
                    self.refresh()
            return True
        if keyval == Gdk.KEY_Delete:
            if self.select_mode:
                self._apply_delete()
            else:
                row = self.selected()
                if row:
                    self.store.delete(row.entry_id)
                    self.refresh()
            return True
        return False


def main() -> int:
    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", lambda a: DejaWindow(a).present())
    return app.run(None)

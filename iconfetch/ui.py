"""IconFetch's window: what each app shows now, the real icon found for it, and one button to apply."""

from __future__ import annotations

import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID
from .apps import GENERIC, LOWRES, MISSING, App, installed_apps
from .install import REMOVED, RESTORED, UPDATED, apply, revert, suggested, sync
from .sources import Candidate, Finder, to_png

ICON = 40
WORKERS = 16  # apps looked up at once
# Oldest supported: GTK 4.6 and libadwaita 1.1 (Ubuntu 22.04, Debian 12).
MIN_GTK = (4, 6)
MIN_ADW = (1, 1)


def _spinner(size: int = 32) -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):  # libadwaita 1.6
        return Adw.Spinner(width_request=size, height_request=size)
    return Gtk.Spinner(spinning=True, width_request=size, height_request=size, halign=Gtk.Align.CENTER)


def _frame(header: Gtk.Widget, content: Gtk.Widget) -> Gtk.Widget:
    """A header bar over the content."""
    if hasattr(Adw, "ToolbarView"):  # libadwaita 1.4
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        return view
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.set_vexpand(True)
    box.append(header)
    box.append(content)
    return box


def _texture(png: bytes) -> Gdk.Texture:
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))


def _current_image(app: App) -> Gtk.Image:
    """The icon the app shows now. Decoded like the finds, since GTK alone can't read some (.ico)."""
    image = Gtk.Image(pixel_size=ICON, tooltip_text="Now")
    try:
        with open(app.state.path, "rb") as handle:
            image.set_from_paintable(_texture(to_png(handle.read(), ICON * 2)[0]))
    except (TypeError, OSError, ValueError):
        image.set_from_icon_name("application-x-executable")
    return image


class AppRow(Adw.ActionRow):
    """One app: its icon now, and the icons found for it (a spinner while still looking)."""

    def __init__(self, app: App, found: Optional[list[Candidate]], on_toggle) -> None:
        super().__init__(title=GLib.markup_escape_text(app.name))
        self.app = app
        self.found: list[Candidate] = []
        self.done = False
        self.choice = 0
        self.chooser: Optional[Gtk.MenuButton] = None
        self.touched = False  # ticked or unticked by the person: a late find leaves it be
        self._quiet = False
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=False)
        self.check.connect("toggled", self._toggled, on_toggle)
        # Each prefix goes in front of the last: the box ends up first.
        self.add_prefix(_current_image(app))
        self.add_prefix(self.check)
        self.set_activatable_widget(self.check)

        self.arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self.arrow.add_css_class("dim-label")
        self.add_suffix(self.arrow)
        self.end = Gtk.Box(valign=Gtk.Align.CENTER)
        self.add_suffix(self.end)
        self.picture = Gtk.Image(pixel_size=ICON)
        if found is None:
            self.end.append(_spinner(24))
            self.set_subtitle(GLib.markup_escape_text(f"{app.state.text}  ·  looking…"))
        else:
            self.set_found(found)

    def _toggled(self, _check, on_toggle) -> None:
        if not self._quiet:
            self.touched = True
        on_toggle()

    def set_tick(self, active: bool) -> None:
        self._quiet = True
        self.check.set_active(active)
        self._quiet = False

    def set_found(self, found: list[Candidate]) -> None:
        """Show what the search found for this app."""
        self.found, self.choice, self.done = found, 0, True
        child = self.end.get_first_child()
        while child is not None:
            self.end.remove(child)
            child = self.end.get_first_child()
        self.chooser = None
        if not found:
            self.check.set_sensitive(False)
            self.arrow.set_visible(False)
            self.set_subtitle(GLib.markup_escape_text(f"{self.app.state.text}  ·  no icon found online"))
            return
        if len(found) > 1:
            # Every find, to pick from: Flathub's, the website's, a theme's…
            self.chooser = Gtk.MenuButton(child=self.picture, valign=Gtk.Align.CENTER, popover=self._options())
            self.chooser.add_css_class("flat")
            self.end.append(self.chooser)
        else:
            self.end.append(self.picture)
        self.check.set_sensitive(True)
        self.arrow.set_visible(True)
        if not self.touched:
            self.set_tick(suggested(self.app, self.candidate))
        self._show_choice()

    def _options(self) -> Gtk.Popover:
        grid = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=min(len(self.found), 3),
            max_children_per_line=min(len(self.found), 3),
            homogeneous=True,
        )
        for index, cand in enumerate(self.found):
            image = Gtk.Image(pixel_size=56)
            image.set_from_paintable(_texture(cand.png))
            source = Gtk.Label(label=cand.source, css_classes=["caption-heading"])
            quality = Gtk.Label(label=cand.quality, css_classes=["caption", "dim-label"])
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                          margin_start=6, margin_end=6)
            for child in (image, source, quality):
                box.append(child)
            button = Gtk.Button(child=box, tooltip_text=cand.origin)
            button.add_css_class("flat")
            button.connect("clicked", self._pick, index)
            grid.append(button)
        return Gtk.Popover(child=grid)

    @property
    def candidate(self) -> Optional[Candidate]:
        return self.found[self.choice] if self.found else None

    @property
    def selected(self) -> bool:
        return self.check.get_active() and self.candidate is not None

    def _pick(self, _button, index: int) -> None:
        self.choice = index
        if self.chooser is not None:
            self.chooser.popdown()
        self._show_choice()
        self.touched = True
        self.check.set_active(True)

    def _show_choice(self) -> None:
        cand = self.candidate
        self.picture.set_from_paintable(_texture(cand.png))
        tip = f"{cand.source}: {cand.origin}"
        more = ""
        if len(self.found) > 1:
            tip += f"\nClick to see all {len(self.found)} options"
            more = f"  ·  {len(self.found) - 1} more"
        self.picture.set_tooltip_text(tip)
        self.set_subtitle(GLib.markup_escape_text(f"{self.app.state.text}  →  {cand.text}{more}"))


class AppliedRow(Adw.ActionRow):
    def __init__(self, app: App, on_undo) -> None:
        super().__init__(title=GLib.markup_escape_text(app.name), subtitle="Icon set by IconFetch")
        self.add_prefix(_current_image(app))
        undo = Gtk.Button(label="Undo", valign=Gtk.Align.CENTER, tooltip_text="Put the original icon back")
        undo.connect("clicked", lambda *_: on_undo([app.id]))
        self.add_suffix(undo)


class Window(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="IconFetch", default_width=660, default_height=760)
        self.set_size_request(360, 400)
        self.apps: list[App] = []
        self.found: dict[str, list[Candidate]] = {}
        self.rows: list[AppRow] = []
        self.row_by_id: dict[str, AppRow] = {}
        self.list_page: Optional[Gtk.Widget] = None
        self.nothing_group: Optional[Adw.PreferencesGroup] = None
        self.busy = False

        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="IconFetch")
        header.set_title_widget(self.title_widget)
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look again online (Ctrl+R)")
        self.refresh_button.connect("clicked", lambda *_: self.fetch(fresh=True))
        header.pack_start(self.refresh_button)
        self.apply_button = Gtk.Button(label="Apply", sensitive=False)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self.apply_selected())
        header.pack_end(self.apply_button)

        self.progress = Adw.StatusPage(title="Reading your apps", child=_spinner())
        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.progress, "loading")
        self.toasts.set_child(self.stack)

        self.set_content(_frame(header, self.toasts))

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.fetch(fresh=True))
        self.add_action(refresh)
        undo = Gio.SimpleAction.new("undo", GLib.VariantType.new("as"))
        undo.connect("activate", lambda _action, ids: self.undo(ids.unpack()))
        self.add_action(undo)
        application.set_accels_for_action("win.refresh", ["<Control>r", "F5"])

    # --- Looking -------------------------------------------------------------

    def fetch(self, fresh: bool = False) -> None:
        """List the apps at once, then fill in each one's finds as they arrive.

        Finds come from the download cache when they're less than a week old;
        `fresh` (Look again) asks every source again.
        """
        if self.busy:
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.title_widget.set_subtitle("Looking for icons…")
        if fresh:
            self.found = {}
        threading.Thread(target=self._fetch_all, args=(fresh,), daemon=True).start()

    def _fetch_all(self, fresh: bool) -> None:
        # First catch up with app updates and removals since IconFetch last looked.
        try:
            synced = sync()
        except (OSError, GLib.Error) as error:
            print(f"iconfetch: couldn't refresh its copies: {error}", file=sys.stderr)
            synced = []
        apps = installed_apps()
        GLib.idle_add(self._listed, apps)
        found: dict[str, list[Candidate]] = {}
        failures: dict[str, int] = {}
        # Apps IconFetch hasn't changed first: those are the ones to choose for.
        order = sorted(apps, key=lambda app: app.applied)
        with Finder(fresh=fresh) as finder, ThreadPoolExecutor(max_workers=WORKERS) as pool:
            jobs = {pool.submit(finder.candidates, app): app for app in order}
            for done, job in enumerate(as_completed(jobs), start=1):
                app = jobs[job]
                try:
                    cands, problems = job.result()
                except Exception:  # a bug in one source mustn't leave the window looking forever
                    traceback.print_exc()
                    cands, problems = [], []
                found[app.id] = cands
                for problem in problems:
                    source = problem.split(":", 1)[0]
                    failures[source] = failures.get(source, 0) + 1
                GLib.idle_add(self._found_one, app.id, cands, done, len(apps))
        # A source that failed for most apps is down or out of reach, not just missing one app.
        down = [source for source, count in failures.items() if count > len(apps) / 2]
        GLib.idle_add(self._fetched, apps, found, down, synced)

    def _listed(self, apps: list[App]) -> bool:
        self.apps = apps
        self.show_apps(apps)
        return False

    def _found_one(self, app_id: str, cands: list[Candidate], done: int, total: int) -> bool:
        self.found[app_id] = cands
        row = self.row_by_id.get(app_id)
        if row is not None:
            row.set_found(cands)
            if not cands and row.app.state.status not in (MISSING, GENERIC, LOWRES):
                row.set_visible(False)  # has its own icon and nothing better: listed at the end
        self.title_widget.set_subtitle(f"Checked {done} of {total} apps")
        self._update_apply()
        return False

    def _fetched(self, apps, found, down, synced=()) -> bool:
        self.found = found
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.title_widget.set_subtitle("")
        self._fill_nothing()
        if down:
            self.toast(f"Couldn't reach {' or '.join(down)}. Showing what the other sources had.")
        if synced:
            self.toast(_synced_text(synced))
        return False

    # --- Showing -------------------------------------------------------------

    def show_apps(self, apps: Optional[list[App]] = None) -> None:
        """Lay out the apps by the icon they show now. Rows still being looked up show a spinner;
        ticks and choices carry over, so it's safe after applying or undoing."""
        apps = apps if apps is not None else installed_apps()
        self.apps = apps
        kept = {row.app.id: (row.touched, row.check.get_active(), row.choice) for row in self.rows}
        self.rows, self.row_by_id = [], {}
        page = Adw.PreferencesPage()

        applied = [a for a in apps if a.applied]
        needs = [a for a in apps if not a.applied and a.state.status in (MISSING, GENERIC, LOWRES)]
        own = [a for a in apps if not a.applied and a not in needs]
        if needs:
            page.add(self._group("Needs a better icon", "These show a stand-in or a blurry icon now.", needs, kept))
        if own:
            page.add(self._group("Has its own icon", "Tick any you'd rather swap for one found online.", own, kept))
        if applied:
            group = Adw.PreferencesGroup(title="Changed by IconFetch")
            undo_all = Gtk.Button(label="Undo all", valign=Gtk.Align.CENTER)
            undo_all.add_css_class("flat")
            undo_all.connect("clicked", lambda *_: self.undo([a.id for a in applied]))
            group.set_header_suffix(undo_all)
            for app in applied:
                group.add(AppliedRow(app, self.undo))
            page.add(group)
        self.nothing_group = Adw.PreferencesGroup()
        page.add(self.nothing_group)

        old, self.list_page = self.list_page, page
        self.stack.add_child(page)
        self.stack.set_visible_child(page)
        if old is not None:
            self.stack.remove(old)
        if not self.busy:
            self._fill_nothing()
        self._update_apply()

    def _group(self, title: str, description: str, apps: list[App], kept: dict) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        rows = []
        for app in apps:
            row = AppRow(app, self.found.get(app.id), self._update_apply)
            if app.id in kept:
                row.touched, active, choice = kept[app.id]
                if row.found and choice < len(row.found):
                    row.choice = choice
                    row._show_choice()
                if row.touched:
                    row.set_tick(active and bool(row.found))
            if row.done and not row.found and title == "Has its own icon":
                row.set_visible(False)
            rows.append(row)
        toggle = Gtk.Button(label="Select all", valign=Gtk.Align.CENTER)
        toggle.add_css_class("flat")

        def flip(*_):
            ready = [r for r in rows if r.found and r.get_visible()]
            on = not all(r.check.get_active() for r in ready)
            for row in ready:
                row.check.set_active(on)

        toggle.connect("clicked", flip)
        group.set_header_suffix(toggle)
        for row in rows:
            group.add(row)
            self.row_by_id[row.app.id] = row
        self.rows += rows
        return group

    def _fill_nothing(self) -> None:
        """Apps that have their own icon and got no finds: one line at the end, not a row each."""
        if self.nothing_group is None:
            return
        nothing = [r.app for r in self.rows if r.done and not r.found and not r.get_visible()]
        if not nothing:
            return
        expander = Adw.ExpanderRow(
            title=f"No icon found for {len(nothing)} app{'s' * (len(nothing) != 1)}",
            subtitle="They keep their own; no app store, website or icon collection had another",
        )
        for app in nothing:
            row = Adw.ActionRow(title=GLib.markup_escape_text(app.name), subtitle=GLib.markup_escape_text(app.state.text))
            row.add_prefix(_current_image(app))
            expander.add_row(row)
        self.nothing_group.add(expander)

    def _update_apply(self) -> None:
        count = sum(row.selected for row in self.rows)
        self.apply_button.set_label(f"Apply {count}" if count else "Apply")
        self.apply_button.set_sensitive(count > 0)

    # --- Changing ------------------------------------------------------------

    def apply_selected(self) -> None:
        chosen = [row for row in self.rows if row.selected]
        done, failed = [], []
        for row in chosen:
            try:
                apply(row.app, row.candidate)
                done.append(row.app.id)
            except (OSError, GLib.Error) as error:
                failed.append(f"{row.app.name}: {getattr(error, 'message', error)}")
        self.show_apps()
        if failed:
            print("\n".join(failed), file=sys.stderr)
            self.toast(f"Couldn't change {len(failed)} app{'s' * (len(failed) != 1)}: {failed[0]}")
        if done:
            self.toast(f"{len(done)} icon{'s' * (len(done) != 1)} applied", undo=done)

    def undo(self, ids: list[str]) -> None:
        wanted = set(ids)
        for app in installed_apps():
            if app.id in wanted and app.applied:
                try:
                    revert(app)
                except (OSError, GLib.Error) as error:
                    self.toast(f"Couldn't undo {app.name}: {getattr(error, 'message', error)}")
        self.show_apps()

    def toast(self, text: str, undo: Optional[list[str]] = None) -> None:
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=6)
        if undo:
            # An action rather than the button-clicked signal, which libadwaita 1.1 lacks.
            toast.set_button_label("Undo")
            toast.set_action_name("win.undo")
            toast.set_action_target_value(GLib.Variant("as", undo))
        self.toasts.add_toast(toast)


def _synced_text(synced) -> str:
    """'Caught up with your apps: 1 updated, 2 uninstalled.'"""
    count = {kind: sum(1 for _name, k in synced if k == kind) for kind in (UPDATED, REMOVED, RESTORED)}
    parts = []
    if count[UPDATED]:
        parts.append(f"{count[UPDATED]} updated (icon kept)")
    if count[REMOVED]:
        parts.append(f"{count[REMOVED]} uninstalled (copy removed)")
    if count[RESTORED]:
        parts.append(f"{count[RESTORED]} back to the original icon (icon file missing)")
    return "Caught up with your apps: " + ", ".join(parts)


class Application(Adw.Application):
    def __init__(self) -> None:
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)  # GLib < 2.74
        super().__init__(application_id=APP_ID, flags=flags)
        GLib.set_application_name("IconFetch")
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = Window(self)
            window.fetch()
        window.present()


def versions() -> tuple[tuple[int, int], tuple[int, int]]:
    return (Gtk.get_major_version(), Gtk.get_minor_version()), (Adw.get_major_version(), Adw.get_minor_version())


def run() -> int:
    gtk, adw = versions()
    if gtk < MIN_GTK or adw < MIN_ADW:
        print(f"IconFetch needs GTK {'.'.join(map(str, MIN_GTK))}+ and libadwaita {'.'.join(map(str, MIN_ADW))}+; "
              f"this system has GTK {'.'.join(map(str, gtk))} and libadwaita {'.'.join(map(str, adw))}.", file=sys.stderr)
        return 1
    return Application().run([sys.argv[0]])

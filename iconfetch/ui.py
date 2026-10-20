"""IconFetch's window: what each app shows now, the real icon found for it, and one button to apply."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID
from .apps import GENERIC, LOWRES, MISSING, App, installed_apps
from .install import apply, revert, suggested
from .sources import Candidate, Finder, to_png

ICON = 40
WORKERS = 8
# Oldest supported: GTK 4.6 and libadwaita 1.1 (Ubuntu 22.04, Debian 12).
MIN_GTK = (4, 6)
MIN_ADW = (1, 1)


def _spinner() -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):  # libadwaita 1.6
        return Adw.Spinner(width_request=32, height_request=32)
    return Gtk.Spinner(spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER)


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
    """One app: its icon now, and the icon found for it."""

    def __init__(self, app: App, found: list[Candidate], on_toggle) -> None:
        super().__init__(title=GLib.markup_escape_text(app.name))
        self.app = app
        self.found = found
        self.choice = 0
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=suggested(app, self.candidate))
        self.check.connect("toggled", lambda *_: on_toggle())
        # Each prefix goes in front of the last: the box ends up first.
        self.add_prefix(_current_image(app))
        self.add_prefix(self.check)
        self.set_activatable_widget(self.check)

        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")
        self.add_suffix(arrow)
        self.picture = Gtk.Image(pixel_size=ICON)
        if len(found) > 1:
            # Every find, to pick from: Flathub's, the website's, a theme's…
            self.chooser = Gtk.MenuButton(child=self.picture, valign=Gtk.Align.CENTER, popover=self._options())
            self.chooser.add_css_class("flat")
            self.add_suffix(self.chooser)
        else:
            self.add_suffix(self.picture)
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
        self.chooser.popdown()
        self._show_choice()
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
        self.found: dict[str, list[Candidate]] = {}
        self.rows: list[AppRow] = []
        self.list_page: Optional[Gtk.Widget] = None
        self.busy = False

        header = Adw.HeaderBar()
        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look again (Ctrl+R)")
        self.refresh_button.connect("clicked", lambda *_: self.fetch())
        header.pack_start(self.refresh_button)
        self.apply_button = Gtk.Button(label="Apply", sensitive=False)
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self.apply_selected())
        header.pack_end(self.apply_button)

        self.progress = Adw.StatusPage(title="Looking for icons", child=_spinner())
        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.progress, "loading")
        self.toasts.set_child(self.stack)

        self.set_content(_frame(header, self.toasts))

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.fetch())
        self.add_action(refresh)
        undo = Gio.SimpleAction.new("undo", GLib.VariantType.new("as"))
        undo.connect("activate", lambda _action, ids: self.undo(ids.unpack()))
        self.add_action(undo)
        application.set_accels_for_action("win.refresh", ["<Control>r", "F5"])

    # --- Looking -------------------------------------------------------------

    def fetch(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.apply_button.set_sensitive(False)
        self.progress.set_description("Reading installed apps…")
        self.stack.set_visible_child_name("loading")
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def _fetch_all(self) -> None:
        apps = installed_apps()
        finder = Finder()
        found: dict[str, list[Candidate]] = {}
        failures: dict[str, int] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for app, (cands, problems) in zip(apps, pool.map(finder.candidates, apps)):
                found[app.id] = cands
                for problem in problems:
                    source = problem.split(":", 1)[0]
                    failures[source] = failures.get(source, 0) + 1
                done += 1
                GLib.idle_add(self.progress.set_description, f"Checked {done} of {len(apps)} apps")
        # A source that failed for most apps is down or out of reach, not just missing one app.
        down = [source for source, count in failures.items() if count > len(apps) / 2]
        GLib.idle_add(self._fetched, apps, found, down)

    def _fetched(self, apps, found, down) -> bool:
        self.found = found
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.show_apps(apps)
        if down:
            self.toast(f"Couldn't reach {' or '.join(down)}. Showing what the other sources had.")
        return False

    # --- Showing -------------------------------------------------------------

    def show_apps(self, apps: Optional[list[App]] = None) -> None:
        """Lay out the apps; reuses what was found, so it's quick after applying or undoing."""
        apps = apps if apps is not None else installed_apps()
        self.rows = []
        page = Adw.PreferencesPage()

        applied = [a for a in apps if a.applied]
        offered = [a for a in apps if not a.applied and self.found.get(a.id)]
        needs = [a for a in offered if a.state.status in (MISSING, GENERIC, LOWRES)]
        better = [a for a in offered if a not in needs]
        nothing = [a for a in apps if not a.applied and not self.found.get(a.id)]

        if needs:
            page.add(self._group("Needs a better icon", "These show a stand-in or a blurry icon now.", needs))
        if better:
            page.add(self._group("Official icon available", "These have an icon already. Tick any you'd rather swap.", better))
        if applied:
            group = Adw.PreferencesGroup(title="Changed by IconFetch")
            undo_all = Gtk.Button(label="Undo all", valign=Gtk.Align.CENTER)
            undo_all.add_css_class("flat")
            undo_all.connect("clicked", lambda *_: self.undo([a.id for a in applied]))
            group.set_header_suffix(undo_all)
            for app in applied:
                group.add(AppliedRow(app, self.undo))
            page.add(group)
        if nothing:
            group = Adw.PreferencesGroup()
            expander = Adw.ExpanderRow(
                title=f"No icon found for {len(nothing)} app{'s' * (len(nothing) != 1)}",
                subtitle="No app store, website or icon collection had one",
            )
            for app in nothing:
                row = Adw.ActionRow(title=GLib.markup_escape_text(app.name), subtitle=GLib.markup_escape_text(app.state.text))
                row.add_prefix(_current_image(app))
                expander.add_row(row)
            group.add(expander)
            page.add(group)

        if not (needs or better or applied):
            page = Adw.StatusPage(
                icon_name="emblem-ok-symbolic",
                title="Nothing to fetch",
                description="No official icons were found for your apps.",
            )

        old, self.list_page = self.list_page, page
        self.stack.add_child(page)
        self.stack.set_visible_child(page)
        if old is not None:
            self.stack.remove(old)
        self._update_apply()

    def _group(self, title: str, description: str, apps: list[App]) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        rows = [AppRow(app, self.found[app.id], self._update_apply) for app in apps]
        toggle = Gtk.Button(label="Select all", valign=Gtk.Align.CENTER)
        toggle.add_css_class("flat")

        def flip(*_):
            on = not all(r.check.get_active() for r in rows)
            for row in rows:
                row.check.set_active(on)

        toggle.connect("clicked", flip)
        group.set_header_suffix(toggle)
        for row in rows:
            group.add(row)
        self.rows += rows
        return group

    def _update_apply(self) -> None:
        count = sum(row.selected for row in self.rows)
        self.apply_button.set_label(f"Apply {count}" if count else "Apply")
        self.apply_button.set_sensitive(count > 0 and not self.busy)

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

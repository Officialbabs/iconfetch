"""Installed applications (desktop entries) and the state of their icons."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

from . import APP_ID

GROUP = "Desktop Entry"
# Written into entries IconFetch changed. OVERRIDE is true when IconFetch made
# the whole file (a copy shadowing a system entry), false when it edited one of
# the person's own entries in place.
MARK_OVERRIDE = "X-IconFetch-Override"
MARK_ORIGINAL = "X-IconFetch-Original-Icon"

# Ada's own apps take their icons from the desktop itself. Windows apps Ada
# runs (ada-windows-…) are the person's, so they stay.
ADA_OWN = re.compile(r"^(ada-(?!windows-)|os-ai-)")
ICON_EXTS = (".png", ".svg", ".xpm")
# Freedesktop category names: a stand-in, not the app's own icon.
GENERIC_ICON = re.compile(
    r"^(accessories|applications|preferences|utilities|system|dialog|input|internet|"
    r"multimedia|office|help|folder|text|network|computer|emblem|document|web)(-|$)"
)
LOW_RES = 128
# Programs that start other programs: their name says nothing about the app.
LAUNCHERS = {"os-ai-open", "flatpak", "snap", "env", "gtk-launch", "gio", "xdg-open", "wine", "wine64",
             "sh", "bash", "python", "python3", "java", "electron", "mono", "dotnet", "steam"}
PACKAGING_WORDS = {"linux", "next", "desktop", "bin", "app", "appimage", "git", "nightly", "beta",
                   "stable", "electron", "client", "snap", "flatpak", "x86", "64", "amd64"}
# App names that are ordinary words: matching on them online finds some other
# app called "Files". Such apps match by ID, program or icon name instead.
GENERIC_NAMES = {
    "files", "software", "settings", "info", "terminal", "console", "browser", "webbrowser",
    "calculator", "camera", "photos", "music", "videos", "video", "maps", "weather", "clock",
    "clocks", "calendar", "contacts", "notes", "tasks", "texteditor", "editor", "viewer",
    "documentviewer", "imageviewer", "disks", "systemmonitor", "help", "languagesupport",
    "characters", "fonts", "logs", "screenshot", "mail", "email", "chat", "player",
    "mediaplayer", "recorder", "scanner", "printers", "archive", "archivemanager",
    "passwords", "backup", "boxes", "connections", "tour", "documents", "images", "network",
    "sound", "display", "power", "updates", "store", "appcenter", "inputmethod",
    "advancednetworkconfiguration", "ibuspreferences", "app", "application",
}

MISSING, GENERIC, LOWRES, OWN = "missing", "generic", "lowres", "own"
STATUS_TEXT = {
    MISSING: "No icon",
    GENERIC: "Generic icon",
    LOWRES: "Low-resolution icon",
    OWN: "Own icon",
}


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def data_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [data_home()] + [Path(d) for d in dirs.split(":") if d]


def store_dir() -> Path:
    """Where IconFetch keeps the icons it applied, and backups."""
    return data_home() / "iconfetch"


@dataclass
class IconState:
    status: str
    path: Optional[str] = None
    size: int = -1  # pixels on the short side; 0 = scalable, -1 = unknown
    detail: str = ""

    @property
    def text(self) -> str:
        if self.detail:
            return self.detail
        if self.status == OWN and self.size >= 0:
            return f"{STATUS_TEXT[OWN]} · {'vector' if self.size == 0 else f'{self.size}px'}"
        return STATUS_TEXT[self.status]


@dataclass
class App:
    id: str
    path: Path
    name: str
    icon: str
    exec: str = ""
    wmclass: str = ""
    original_icon: Optional[str] = None  # set when IconFetch changed this entry
    ours: bool = False  # IconFetch made this file
    state: IconState = field(default_factory=lambda: IconState(MISSING))

    @property
    def is_user(self) -> bool:
        return _inside(self.path, data_home() / "applications")

    @property
    def applied(self) -> bool:
        return self.original_icon is not None

    @property
    def stem(self) -> str:
        return self.id.removesuffix(".desktop")

    def match_keys(self) -> set[str]:
        """Normalised names that identify this app: its name, desktop ID, icon name, program, window class."""
        names = [self.name, self.stem, self.wmclass, *self.stem.split("_")]  # snaps: firefox_firefox
        if self.stem.count(".") >= 2:
            names.append(self.stem.rsplit(".", 1)[-1])
        icon = self.original_icon if self.applied else self.icon
        if icon:
            names.append(Path(icon).stem if icon.startswith("/") else icon)
        program = Path(exec_program(self.exec)).name
        if program and program not in LAUNCHERS:
            names.append(program.removesuffix(".AppImage"))
        keys = set()
        for name in names:
            keys.add(norm(name))
            # figma-linux-next is Figma: drop packaging words from the end.
            words = re.split(r"[-_ .]+", name.lower())
            while len(words) > 1 and words[-1] in PACKAGING_WORDS:
                words.pop()
                keys.add(norm("".join(words)))
        keys.discard("")
        return keys - GENERIC_NAMES


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def exec_program(exec_line: str) -> str:
    """The program an Exec line runs, past `env` and VAR=value prefixes."""
    try:
        words = shlex.split(exec_line)
    except ValueError:
        words = exec_line.split()
    words = [w for w in words if not w.startswith("%")]
    while words and (words[0] == "env" or ("=" in words[0] and not words[0].startswith("/"))):
        words.pop(0)
    return words[0] if words else ""


def _inside(path: Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _desktops() -> set[str]:
    return {d for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d}


def _visible(kf: GLib.KeyFile) -> bool:
    def flag(key):
        try:
            return kf.get_boolean(GROUP, key)
        except GLib.Error:
            return False

    def items(key):
        try:
            return set(kf.get_string_list(GROUP, key))
        except GLib.Error:
            return None

    try:
        if kf.get_string(GROUP, "Type") != "Application":
            return False
    except GLib.Error:
        return False
    if flag("NoDisplay") or flag("Hidden"):
        return False
    only, never, here = items("OnlyShowIn"), items("NotShowIn"), _desktops()
    if only is not None and not (only & here):
        return False
    return not (never and never & here)


def has_key(kf: GLib.KeyFile, key: str) -> bool:
    try:
        return key in kf.get_keys(GROUP)[0]
    except GLib.Error:
        return False


def _get(kf: GLib.KeyFile, key: str, localized: bool = False) -> str:
    try:
        return kf.get_locale_string(GROUP, key, None) if localized else kf.get_string(GROUP, key)
    except GLib.Error:
        return ""


def load_entry(desktop_id: str, path: Path) -> Optional[App]:
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(str(path), GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return None
    if not _visible(kf):
        return None
    app = App(
        id=desktop_id,
        path=path,
        name=_get(kf, "Name", localized=True) or desktop_id,
        icon=_get(kf, "Icon"),
        exec=_get(kf, "Exec"),
        wmclass=_get(kf, "StartupWMClass"),
    )
    if has_key(kf, MARK_ORIGINAL):
        app.original_icon = _get(kf, MARK_ORIGINAL)
        try:
            app.ours = kf.get_boolean(GROUP, MARK_OVERRIDE)
        except GLib.Error:
            app.ours = False
    return app


def find_entries() -> dict[str, Path]:
    """Every desktop ID and the file that wins for it (the first data dir has priority)."""
    found: dict[str, Path] = {}
    for base in data_dirs():
        root = base / "applications"
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".desktop"):
                    continue
                path = Path(dirpath) / name
                desktop_id = str(path.relative_to(root)).replace(os.sep, "-")
                found.setdefault(desktop_id, path)
    return found


def installed_apps(include_ada: bool = False) -> list[App]:
    index = IconIndex()
    apps = []
    for desktop_id, path in find_entries().items():
        if desktop_id == f"{APP_ID}.desktop" or (not include_ada and ADA_OWN.match(desktop_id)):
            continue
        app = load_entry(desktop_id, path)
        if app is None:
            continue
        app.state = index.state(app.icon)
        apps.append(app)
    apps.sort(key=lambda a: a.name.casefold())
    return apps


def image_size(path: str) -> int:
    """Short side in pixels, 0 for a vector image, -1 when unreadable."""
    if path.lower().endswith((".svg", ".svgz")):
        return 0
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(path)
    except GLib.Error:
        return -1
    if info is None or info[0] is None:
        return -1
    return min(info[1], info[2])


def _theme_names() -> list[str]:
    names = ["Adwaita", "hicolor"]
    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source and source.lookup("org.gnome.desktop.interface", True):
            theme = Gio.Settings.new("org.gnome.desktop.interface").get_string("icon-theme")
            if theme and theme not in names:
                names.insert(0, theme)
    except GLib.Error:
        pass
    return names


@dataclass
class IconFile:
    path: str
    size: int  # 0 = scalable, -1 = unknown
    context: str  # apps, categories, devices, pixmaps…


class IconIndex:
    """Icon files by name across the icon themes the desktop looks in."""

    def __init__(self) -> None:
        self.files: dict[str, list[IconFile]] = {}
        roots = [Path.home() / ".icons"] + [d / "icons" for d in data_dirs()]
        for root in roots:
            for theme in _theme_names():
                self._scan(root / theme)
        for base in data_dirs():
            pixmaps = base / "pixmaps"
            if pixmaps.is_dir():
                for entry in pixmaps.iterdir():
                    if entry.suffix in ICON_EXTS and entry.is_file():
                        self._add(entry.stem, IconFile(str(entry), -1, "pixmaps"))

    def _scan(self, base: Path) -> None:
        if not base.is_dir():
            return
        for dirpath, _dirs, files in os.walk(base):
            parts = Path(dirpath).relative_to(base).parts
            if not parts or "symbolic" in parts:
                continue
            size = _dir_size(parts)
            if size is None:
                continue
            context = parts[1] if len(parts) > 1 else ""
            for name in files:
                stem, ext = os.path.splitext(name)
                if ext in ICON_EXTS:
                    self._add(stem, IconFile(os.path.join(dirpath, name), size, context))

    def _add(self, name: str, icon: IconFile) -> None:
        self.files.setdefault(name, []).append(icon)

    def best(self, name: str) -> Optional[IconFile]:
        found = self.files.get(name) or self.files.get(Path(name).stem if Path(name).suffix in ICON_EXTS else "")
        if not found:
            return None
        for icon in found:
            if icon.size == -1:
                icon.size = image_size(icon.path)
        return max(found, key=lambda i: (i.size == 0, i.size))

    def state(self, icon: str) -> IconState:
        if not icon:
            return IconState(MISSING, detail="No icon set")
        if icon.startswith("/"):
            if not os.path.exists(icon):
                return IconState(MISSING, detail="Icon file missing")
            size = image_size(icon)
            if 0 < size < LOW_RES:
                return IconState(LOWRES, icon, size, f"Low-resolution icon ({size}px)")
            return IconState(OWN, icon, size)
        best = self.best(icon)
        if best is None:
            return IconState(MISSING, detail="Icon missing")
        contexts = {f.context for f in self.files.get(icon, [])}
        if GENERIC_ICON.match(icon) or not contexts & {"apps", "pixmaps"}:
            return IconState(GENERIC, best.path, best.size)
        if 0 < best.size < LOW_RES:
            return IconState(LOWRES, best.path, best.size, f"Low-resolution icon ({best.size}px)")
        return IconState(OWN, best.path, best.size)


def _dir_size(parts: tuple[str, ...]) -> Optional[int]:
    """Icon size from a theme directory: 48x48, 48x48@2 (Adwaita puts size first)."""
    head = parts[0]
    if head == "scalable":
        return 0
    match = re.fullmatch(r"(\d+)x(\d+)(?:@(\d+))?", head)
    if not match:
        return None
    return int(match.group(1)) * int(match.group(3) or 1)

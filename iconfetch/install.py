"""Apply an icon to an app, and take it back.

The desktop reads entries in ~/.local/share/applications before the system's,
so an entry there with the same desktop ID wins. IconFetch writes the icon to
its own folder under a name that changes with the picture (so nothing serves a
cached copy), then points the entry's Icon at it. GIO's app monitor sees the
new entry and the dock, search and switcher redraw at once.

A copy stands in for the system's entry, so `sync` keeps it in step: when the
app updates its entry, the copy is rebuilt from the new one with the chosen
icon; when the app is uninstalled, the copy goes too.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Union

from gi.repository import GLib

from .apps import (
    GENERIC,
    GROUP,
    LOWRES,
    MARK_ORIGINAL,
    MARK_OVERRIDE,
    MISSING,
    App,
    data_home,
    entry_value,
    has_key,
    program_present,
    store_dir,
    system_entries,
)
from .sources import Candidate

KEEP = GLib.KeyFileFlags.KEEP_COMMENTS | GLib.KeyFileFlags.KEEP_TRANSLATIONS


def icons_dir() -> Path:
    return store_dir() / "icons"


def backups_dir() -> Path:
    return store_dir() / "backup"


def suggested(app: App, cand: Optional[Candidate]) -> bool:
    """Whether to tick an app by default: its icon is missing, generic or blurry, and the find is better."""
    if cand is None or app.applied:
        return False
    if app.state.status in (MISSING, GENERIC):
        return True
    if app.state.status == LOWRES:
        return cand.size == 0 or cand.size > app.state.size
    return False


def _ours(path: str) -> bool:
    try:
        return bool(path) and Path(path).resolve().is_relative_to(icons_dir().resolve())
    except OSError:
        return False


def _write(content: Union[GLib.KeyFile, str], target: Path, mode: Optional[int] = None) -> None:
    text = content if isinstance(content, str) else content.to_data()[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".iconfetch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _copy_text(original: Path, icon: str) -> str:
    """IconFetch's copy of a system entry: the entry as it is now, with `icon`."""
    kf = GLib.KeyFile()
    kf.load_from_file(str(original), KEEP)
    kf.set_string(GROUP, MARK_ORIGINAL, entry_value(kf, "Icon"))
    kf.set_boolean(GROUP, MARK_OVERRIDE, True)
    kf.set_string(GROUP, "Icon", icon)
    return kf.to_data()[0]


def apply(app: App, cand: Candidate) -> Path:
    """Point the app at `cand` now. Returns the entry written."""
    digest = hashlib.sha256(cand.png).hexdigest()[:12]
    icon = icons_dir() / f"{app.stem}-{digest}.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    if not icon.exists():
        tmp = icon.with_suffix(".tmp")
        tmp.write_bytes(cand.png)
        os.replace(tmp, icon)

    previous = app.icon
    original = system_entries().get(app.id) if (app.ours or not app.is_user) else None
    if original is not None:
        # A copy of the system entry that stands in for it under the same desktop ID.
        target = app.path if app.ours else data_home() / "applications" / app.id
        content: Union[GLib.KeyFile, str] = _copy_text(original, str(icon))
    else:
        # The person's own entry, edited in place after a backup.
        target = app.path
        content = GLib.KeyFile()
        content.load_from_file(str(app.path), KEEP)
        if not app.applied:
            backup = backups_dir() / app.id
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(app.path, backup)
            content.set_string(GROUP, MARK_ORIGINAL, app.icon)
            content.set_boolean(GROUP, MARK_OVERRIDE, False)
        content.set_string(GROUP, "Icon", str(icon))
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    _write(content, target, mode)

    if previous != str(icon) and _ours(previous):
        Path(previous).unlink(missing_ok=True)
    _nudge()
    return target


def revert(app: App) -> None:
    """Put the app's own icon back: remove IconFetch's copy, or restore the edited entry."""
    if not app.applied:
        return
    if app.ours:
        app.path.unlink(missing_ok=True)
    else:
        _restore_in_place(app.path, app.id)
    if _ours(app.icon):
        Path(app.icon).unlink(missing_ok=True)
    _nudge()


def _restore_in_place(path: Path, desktop_id: str) -> None:
    """Put the original Icon line back in one of the person's own entries."""
    kf = GLib.KeyFile()
    kf.load_from_file(str(path), KEEP)
    original = entry_value(kf, MARK_ORIGINAL)
    if original:
        kf.set_string(GROUP, "Icon", original)
    elif has_key(kf, "Icon"):
        kf.remove_key(GROUP, "Icon")
    for key in (MARK_ORIGINAL, MARK_OVERRIDE):
        if has_key(kf, key):
            kf.remove_key(GROUP, key)
    _write(kf, path, path.stat().st_mode & 0o777)
    (backups_dir() / desktop_id).unlink(missing_ok=True)


UPDATED, REMOVED, RESTORED = "updated", "removed", "restored"
SYNC_TEXT = {
    UPDATED: "the app updated its entry; IconFetch's copy now matches it, with the same icon",
    REMOVED: "the app is gone; removed IconFetch's copy of its entry",
    RESTORED: "IconFetch's icon file was missing; put the app's own icon back",
}


def sync() -> list[tuple[str, str]]:
    """Bring IconFetch's changes in line with the system, and say what changed (app name, UPDATED…).

    A copy whose system entry changed is rebuilt from it, keeping the chosen icon.
    A copy whose app was uninstalled (no system entry in any app folder, and no
    program) is removed, so no broken launcher stays behind. A change whose icon file has gone is undone. Icons no entry uses
    any more are deleted.
    """
    folder = data_home() / "applications"
    if not folder.is_dir():
        return []
    done: list[tuple[str, str]] = []
    originals: Optional[dict[str, Path]] = None
    in_use: set[str] = set()
    unreadable = False
    for path in sorted(folder.rglob("*.desktop")):
        kf = GLib.KeyFile()
        try:
            kf.load_from_file(str(path), KEEP)
        except GLib.Error:
            unreadable = True
            continue
        if not has_key(kf, MARK_ORIGINAL):
            continue
        desktop_id = str(path.relative_to(folder)).replace(os.sep, "-")
        name = entry_value(kf, "Name", localized=True) or desktop_id
        icon = entry_value(kf, "Icon")
        icon_gone = _ours(icon) and not os.path.exists(icon)
        try:
            ours = kf.get_boolean(GROUP, MARK_OVERRIDE)
        except GLib.Error:
            ours = False
        if not ours:
            if icon_gone:
                _restore_in_place(path, desktop_id)
                done.append((name, RESTORED))
            else:
                in_use.add(icon)
            continue
        if originals is None:
            originals = system_entries()
        original = originals.get(desktop_id)
        if original is None and program_present(entry_value(kf, "Exec"), entry_value(kf, "TryExec")):
            in_use.add(icon)  # no system entry under this ID, but the program is there: leave it be
            continue
        if original is None or icon_gone:
            path.unlink(missing_ok=True)
            if _ours(icon):
                Path(icon).unlink(missing_ok=True)
            done.append((name, REMOVED if original is None else RESTORED))
            continue
        in_use.add(icon)
        try:
            expected = _copy_text(original, icon)
        except GLib.Error:
            continue  # the system entry is mid-update; the next sync sees the finished one
        if path.read_text(encoding="utf-8") != expected:
            _write(expected, path, path.stat().st_mode & 0o777)
            updated = GLib.KeyFile()
            updated.load_from_data(expected, len(expected.encode("utf-8")), GLib.KeyFileFlags.NONE)
            done.append((entry_value(updated, "Name", localized=True) or name, UPDATED))
    # Icons no entry points at (an entry it couldn't read might, so then keep them all).
    if not unreadable and icons_dir().is_dir():
        for file in icons_dir().glob("*.png"):
            if str(file) not in in_use and not any(_same(file, used) for used in in_use):
                file.unlink(missing_ok=True)
    if done:
        _nudge()
    return done


def _same(file: Path, other: str) -> bool:
    try:
        return file.samefile(other)
    except OSError:
        return False


def _nudge() -> None:
    """Nudge anything that caches desktop entries. GIO's monitor already sees the file change."""
    apps = data_home() / "applications"
    if not apps.is_dir():
        return
    os.utime(apps)
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, "-q", str(apps)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

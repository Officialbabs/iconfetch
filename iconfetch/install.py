"""Apply an icon to an app, and take it back.

The desktop reads entries in ~/.local/share/applications before the system's,
so an entry there with the same desktop ID wins. IconFetch writes the icon to
its own folder under a name that changes with the picture (so nothing serves a
cached copy), then points the entry's Icon at it. GIO's app monitor sees the
new entry and Ada's dock, search and switcher redraw at once.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

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
    has_key,
    store_dir,
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


def _write(kf: GLib.KeyFile, target: Path, mode: Optional[int] = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".iconfetch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(kf.to_data()[0])
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply(app: App, cand: Candidate) -> Path:
    """Point the app at `cand` now. Returns the entry written."""
    digest = hashlib.sha256(cand.png).hexdigest()[:12]
    icon = icons_dir() / f"{app.stem}-{digest}.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    if not icon.exists():
        tmp = icon.with_suffix(".tmp")
        tmp.write_bytes(cand.png)
        os.replace(tmp, icon)

    kf = GLib.KeyFile()
    kf.load_from_file(str(app.path), KEEP)
    previous = app.icon
    if app.is_user:
        target = app.path
        if not app.applied:
            backup = backups_dir() / app.id
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(app.path, backup)
            kf.set_string(GROUP, MARK_ORIGINAL, app.icon)
            kf.set_boolean(GROUP, MARK_OVERRIDE, False)
    else:
        # A copy of the system entry that shadows it under the same desktop ID.
        target = data_home() / "applications" / app.id
        kf.set_string(GROUP, MARK_ORIGINAL, app.icon)
        kf.set_boolean(GROUP, MARK_OVERRIDE, True)
    kf.set_string(GROUP, "Icon", str(icon))
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    _write(kf, target, mode)

    if previous != str(icon) and _ours(previous):
        Path(previous).unlink(missing_ok=True)
    refresh()
    return target


def revert(app: App) -> None:
    """Put the app's own icon back: remove IconFetch's copy, or restore the edited entry."""
    if not app.applied:
        return
    if app.ours:
        app.path.unlink(missing_ok=True)
    else:
        kf = GLib.KeyFile()
        kf.load_from_file(str(app.path), KEEP)
        if app.original_icon:
            kf.set_string(GROUP, "Icon", app.original_icon)
        elif has_key(kf, "Icon"):
            kf.remove_key(GROUP, "Icon")
        for key in (MARK_ORIGINAL, MARK_OVERRIDE):
            if has_key(kf, key):
                kf.remove_key(GROUP, key)
        _write(kf, app.path, app.path.stat().st_mode & 0o777)
        (backups_dir() / app.id).unlink(missing_ok=True)
    if _ours(app.icon):
        Path(app.icon).unlink(missing_ok=True)
    refresh()


def refresh() -> None:
    """Nudge anything that caches desktop entries. GIO's monitor already sees the file change."""
    apps = data_home() / "applications"
    if not apps.is_dir():
        return
    os.utime(apps)
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, "-q", str(apps)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

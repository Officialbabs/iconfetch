"""Opens IconFetch's window with made-up apps and finds, clicks through it, and exits 0 if nothing broke.

Needs a display:  xvfb-run -a python3 tests/smoke_ui.py [--old-adw]

--old-adw hides the libadwaita widgets newer than 1.1 (ToolbarView, Spinner),
so the fallbacks used on Ubuntu 22.04 and Debian 12 run here too. Nothing
outside a throwaway data folder is touched.
"""

import os
import sys
import time
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = tempfile.mkdtemp(prefix="iconfetch-smoke-")
os.environ["XDG_DATA_HOME"] = HOME

from gi.repository import Gio, GLib  # noqa: E402

from iconfetch import ui  # noqa: E402
from iconfetch.apps import installed_apps  # noqa: E402
from iconfetch.sources import Candidate, to_png  # noqa: E402

SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" rx="4" fill="{}"/></svg>'
STEPS_MS = 400


class OldAdw:
    """libadwaita as 1.1 had it."""

    real = ui.Adw

    def __getattr__(self, name):
        if name in ("ToolbarView", "Spinner"):
            raise AttributeError(name)
        return getattr(self.real, name)


class FakeFinder:
    def __init__(self, fresh=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def candidates(self, app):
        if app.id == "smoke-own.desktop":
            time.sleep(1.5)  # a slow source: the window must not wait for it
        found = [Candidate(source, f"smoke/{source}", to_png(SVG.format(colour).encode())[0], 0)
                 for source, colour in (("Flathub", "#36c"), ("Papirus theme", "#e63"))]
        return found, []


def main() -> int:
    if "--old-adw" in sys.argv:
        ui.Adw = OldAdw()
    apps_dir = Path(HOME, "applications")
    apps_dir.mkdir(parents=True)
    for name, icon in (("Smoke Missing", "smoke-no-such-icon"), ("Smoke Own", "")):
        stem = "smoke-" + name.split()[-1].lower()
        (apps_dir / f"{stem}.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec=true\n" + (f"Icon={icon}\n" if icon else "")
        )
    ui.installed_apps = lambda: [a for a in installed_apps() if a.id.startswith("smoke-")]
    ui.Finder = FakeFinder

    application = ui.Application()
    application.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    result = {"code": 1}
    original_fetched = ui.Window._fetched
    original_listed = ui.Window._listed
    seen = {}

    def listed(self, apps):
        original_listed(self, apps)

        def look():
            seen["early"] = self.busy and len(self.rows) == 2 and any(not r.done for r in self.rows)
            return False

        GLib.timeout_add(300, look)
        return False

    ui.Window._listed = listed

    def check(condition, what):
        if not condition:
            raise AssertionError(what)
        print("ok:", what, flush=True)

    def finish(code):
        result["code"] = code
        application.quit()
        return False

    def steps(window):
        try:
            check(seen.get("early"), "the list shows while a slow search is still running")
            check(len(window.rows) == 2, "both apps are listed")
            own = next(r for r in window.rows if r.app.id == "smoke-own.desktop")
            own.check.set_active(False)
            missing = next(r for r in window.rows if r.app.id == "smoke-missing.desktop")
            check(missing.check.get_active(), "the app with no icon is ticked")
            check(window.apply_button.get_sensitive(), "Apply is available")
            missing.chooser.popup()
            yield
            missing._pick(None, 1)
            check(missing.candidate.source == "Papirus theme", "the picker switches the choice")
            window.apply_selected()
            entry = (apps_dir / "smoke-missing.desktop").read_text()
            check(f"Icon={HOME}/iconfetch/icons/" in entry, "Apply points the entry at the fetched icon")
            own = next(r for r in window.rows if r.app.id == "smoke-own.desktop")
            check(not own.check.get_active(), "an app the person unticked stays unticked after Apply")
            yield
            # The way the toast's Undo button does it.
            window.activate_action("win.undo", GLib.Variant("as", ["smoke-missing.desktop"]))
            check("Icon=smoke-no-such-icon" in (apps_dir / "smoke-missing.desktop").read_text(), "Undo restores it")
            finish(0)
        except Exception:
            traceback.print_exc()
            finish(1)

    def fetched(self, *args):
        original_fetched(self, *args)
        walk = steps(self)

        def advance():
            try:
                next(walk)
                return True
            except StopIteration:
                return False

        GLib.timeout_add(STEPS_MS, advance)
        return False

    ui.Window._fetched = fetched
    GLib.timeout_add_seconds(30, lambda: (print("timed out", file=sys.stderr), finish(1)))
    application.run([sys.argv[0]])
    gtk, adw = ui.versions()
    mode = " (as libadwaita 1.1)" if "--old-adw" in sys.argv else ""
    print(f"GTK {gtk[0]}.{gtk[1]}, libadwaita {adw[0]}.{adw[1]}{mode}: {'passed' if result['code'] == 0 else 'FAILED'}")
    return result["code"]


if __name__ == "__main__":
    sys.exit(main())

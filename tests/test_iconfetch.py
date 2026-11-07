"""IconFetch tests. Every test runs in its own XDG data folders: nothing touches the real desktop."""

import atexit
import base64
import contextlib
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

from iconfetch import apps as apps_mod
from iconfetch.__main__ import main
from iconfetch.apps import GENERIC, LOWRES, MISSING, OWN, IconIndex, exec_program, installed_apps
from iconfetch.install import REMOVED, RESTORED, UPDATED, apply, icons_dir, revert, suggested, sync
from iconfetch import shapes, sources
from iconfetch.sources import BRANDS, FLATHUB, PAPIRUS, SNAP, Candidate, Finder, IconLinks, Web, _icon_urls, folder_candidates, to_png

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#f60"/></svg>'
WIDE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#06f"/></svg>'
# 1×1 transparent PNG
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


# GdkPixbuf's glycin loaders (SVG among them) and the MIME database they sniff
# with are found through XDG_DATA_DIRS. GLib reads it once, the first time it's
# needed, and checks the MIME database again every few seconds: one folder of
# them for the whole run, so a test's folder being deleted can't take them away.
LOADERS = Path(tempfile.mkdtemp(prefix="iconfetch-loaders-"))
for _name in ("glycin-loaders", "mime"):
    if Path("/usr/share", _name).is_dir():
        (LOADERS / _name).symlink_to(Path("/usr/share", _name))
atexit.register(shutil.rmtree, LOADERS, True)


def entry(name, icon=None, exec_line="app", extra=""):
    lines = ["# kept comment", "[Desktop Entry]", "Type=Application", f"Name={name}", f"Name[de]={name} DE", f"Exec={exec_line}"]
    if icon is not None:
        lines.append(f"Icon={icon}")
    return "\n".join(lines) + "\n" + extra


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.system = root / "system"
        for d in (self.home / "applications", self.system / "applications", self.system / "icons/hicolor"):
            d.mkdir(parents=True)
        self.env = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CURRENT_DESKTOP", "XDG_CACHE_HOME")}
        os.environ["XDG_DATA_HOME"] = str(self.home)
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        os.environ["XDG_DATA_DIRS"] = f"{self.system}:{LOADERS}"
        os.environ["XDG_CURRENT_DESKTOP"] = "Ada"

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def system_entry(self, file, text):
        (self.system / "applications" / file).write_text(text)

    def user_entry(self, file, text):
        (self.home / "applications" / file).write_text(text)

    def theme_icon(self, size, context, name, data=SVG):
        folder = self.system / "icons/hicolor" / size / context
        folder.mkdir(parents=True, exist_ok=True)
        ext = ".svg" if size == "scalable" else ".png"
        (folder / (name + ext)).write_bytes(data)

    def app(self, desktop_id):
        return next(a for a in installed_apps() if a.id == desktop_id)

    def cand(self, data=SVG):
        png, size = to_png(data)
        return Candidate("Flathub", "org.example.App", png, size)


class Entries(Sandbox):
    def test_visibility_and_precedence(self):
        self.system_entry("shown.desktop", entry("Shown"))
        self.system_entry("hidden.desktop", entry("Hidden", extra="NoDisplay=true\n"))
        self.system_entry("gnome-only.desktop", entry("GNOME", extra="OnlyShowIn=GNOME;\n"))
        self.system_entry("ada-chat.desktop", entry("Ada Chat"))
        self.system_entry("ada-windows-abc.desktop", entry("Notepad++"))
        self.system_entry("mine.desktop", entry("System copy"))
        self.user_entry("mine.desktop", entry("User copy"))
        names = {a.id: a.name for a in installed_apps()}
        self.assertEqual(names, {"shown.desktop": "Shown", "ada-windows-abc.desktop": "Notepad++", "mine.desktop": "User copy"})

    def test_icon_states(self):
        self.theme_icon("scalable", "apps", "crisp")
        self.theme_icon("48x48", "apps", "blurry", PNG_1PX)
        self.theme_icon("scalable", "categories", "preferences-color")
        index = IconIndex()
        self.assertEqual(index.state("").status, MISSING)
        self.assertEqual(index.state("not-there").status, MISSING)
        self.assertEqual(index.state("/no/such/file.png").status, MISSING)
        self.assertEqual(index.state("crisp").status, OWN)
        self.assertEqual(index.state("blurry").status, LOWRES)
        self.assertEqual(index.state("preferences-color").status, GENERIC)

    def test_exec_program(self):
        self.assertEqual(exec_program("env FOO=1 /opt/zen/zen %u"), "/opt/zen/zen")
        self.assertEqual(exec_program('"/home/x/My App/run" --flag %F'), "/home/x/My App/run")


class Images(unittest.TestCase):
    def test_vector_renders_at_full_size(self):
        png, size = to_png(SVG)
        self.assertEqual(size, 0)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        from gi.repository import GdkPixbuf

        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        self.assertEqual((loader.get_pixbuf().get_width(), loader.get_pixbuf().get_height()), (256, 256))

    def test_wide_image_is_padded_square(self):
        png, _ = to_png(WIDE_SVG)
        from gi.repository import GdkPixbuf

        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        pix = loader.get_pixbuf()
        self.assertEqual(pix.get_width(), pix.get_height())

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            to_png(b"<html>not found</html>")


class ApplyRevert(Sandbox):
    def test_system_entry_gets_a_shadow_copy_and_undo_removes_it(self):
        self.system_entry("org.example.App.desktop", entry("Example", icon="missing-icon"))
        app = self.app("org.example.App.desktop")
        self.assertTrue(suggested(app, self.cand()))
        written = apply(app, self.cand())
        self.assertEqual(written, self.home / "applications/org.example.App.desktop")

        now = self.app("org.example.App.desktop")
        self.assertTrue(now.applied and now.ours)
        self.assertEqual(now.original_icon, "missing-icon")
        self.assertTrue(now.icon.startswith(str(icons_dir())) and os.path.exists(now.icon))
        self.assertEqual(now.state.status, OWN)
        text = written.read_text()
        self.assertIn("Name[de]=Example DE", text)
        self.assertIn("# kept comment", text)
        self.assertFalse(suggested(now, self.cand()))

        revert(now)
        self.assertFalse(written.exists())
        self.assertEqual(list(icons_dir().iterdir()), [])
        back = self.app("org.example.App.desktop")
        self.assertFalse(back.applied)
        self.assertEqual(back.icon, "missing-icon")

    def test_user_entry_is_edited_in_place_and_restored(self):
        original = entry("Mine", icon="preferences-color", exec_line="/home/me/bin/mine")
        self.user_entry("mine.desktop", original)
        path = self.home / "applications/mine.desktop"
        os.chmod(path, 0o755)
        apply(self.app("mine.desktop"), self.cand())
        self.assertTrue((self.home / "iconfetch/backup/mine.desktop").exists())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)

        # Applying again swaps the picture and drops the old file.
        apply(self.app("mine.desktop"), self.cand(WIDE_SVG))
        self.assertEqual(len(list(icons_dir().iterdir())), 1)
        self.assertEqual(self.app("mine.desktop").original_icon, "preferences-color")

        revert(self.app("mine.desktop"))
        restored = self.app("mine.desktop")
        self.assertFalse(restored.applied)
        self.assertEqual(restored.icon, "preferences-color")
        self.assertNotIn("X-IconFetch", path.read_text())
        self.assertFalse((self.home / "iconfetch/backup/mine.desktop").exists())

    def test_entry_without_icon_key(self):
        self.user_entry("bare.desktop", entry("Bare"))
        apply(self.app("bare.desktop"), self.cand())
        revert(self.app("bare.desktop"))
        self.assertNotIn("Icon=", (self.home / "applications/bare.desktop").read_text())


class Sync(Sandbox):
    """IconFetch's copies follow the apps they stand in for."""

    ID = "org.example.App.desktop"

    def setUp(self):
        super().setUp()
        # Only the sandbox's app folders, never the real /usr/share.
        self.known = apps_mod.KNOWN_APP_DIRS
        apps_mod.KNOWN_APP_DIRS = []

    def tearDown(self):
        apps_mod.KNOWN_APP_DIRS = self.known
        super().tearDown()

    def applied_copy(self, exec_line="/nonexistent/example %u"):
        self.system_entry(self.ID, entry("Example", icon="missing-icon", exec_line=exec_line))
        apply(self.app(self.ID), self.cand())
        return self.home / "applications" / self.ID

    def test_a_copy_follows_the_apps_update_and_keeps_its_icon(self):
        copy = self.applied_copy()
        icon = self.app(self.ID).icon
        action = "Actions=new;\n\n[Desktop Action new]\nName=New window\nExec=/nonexistent/example --window\n"
        self.system_entry(self.ID, entry("Example 2", icon="new-icon", exec_line="/nonexistent/example --new %u", extra=action))
        self.assertEqual(sync(), [("Example 2", UPDATED)])
        now = self.app(self.ID)
        self.assertEqual(now.icon, icon)
        self.assertTrue(now.ours)
        self.assertEqual(now.original_icon, "new-icon")
        self.assertIn("--new", now.exec)
        self.assertIn("[Desktop Action new]", copy.read_text())
        self.assertEqual(sync(), [])

    def test_an_unchanged_copy_is_left_alone(self):
        copy = self.applied_copy()
        before = copy.stat().st_mtime_ns
        self.assertEqual(sync(), [])
        self.assertEqual(copy.stat().st_mtime_ns, before)

    def test_an_uninstalled_apps_copy_goes_with_its_icon(self):
        copy = self.applied_copy()
        icon = self.app(self.ID).icon
        (self.system / "applications" / self.ID).unlink()
        self.assertEqual(sync(), [("Example", REMOVED)])
        self.assertFalse(copy.exists())
        self.assertFalse(os.path.exists(icon))
        self.assertEqual(installed_apps(), [])

    def test_kept_while_the_program_is_still_installed(self):
        program = Path(self.tmp.name) / "bin" / "example"
        program.parent.mkdir()
        program.write_text("#!/bin/sh\n")
        copy = self.applied_copy(exec_line=f"{program} %u")
        (self.system / "applications" / self.ID).unlink()
        self.assertEqual(sync(), [])
        self.assertTrue(copy.exists())

    def test_an_app_folder_missing_from_the_environment_still_counts(self):
        copy = self.applied_copy()
        # As in a bare background service: the app's folder isn't in XDG_DATA_DIRS,
        # but it is one of the standard app folders.
        os.environ["XDG_DATA_DIRS"] = str(LOADERS)
        apps_mod.KNOWN_APP_DIRS = [str(self.system)]
        self.assertEqual(sync(), [])
        self.assertTrue(copy.exists())

    def test_a_missing_icon_file_undoes_the_change(self):
        copy = self.applied_copy()
        os.unlink(self.app(self.ID).icon)
        self.assertEqual(sync(), [("Example", RESTORED)])
        self.assertFalse(copy.exists())
        self.assertEqual(self.app(self.ID).icon, "missing-icon")

        self.user_entry("mine.desktop", entry("Mine", icon="preferences-color"))
        apply(self.app("mine.desktop"), self.cand())
        os.unlink(self.app("mine.desktop").icon)
        self.assertEqual(sync(), [("Mine", RESTORED)])
        mine = self.app("mine.desktop")
        self.assertEqual(mine.icon, "preferences-color")
        self.assertFalse(mine.applied)

    def test_unused_icons_are_deleted_and_used_ones_kept(self):
        self.applied_copy()
        stray = icons_dir() / "uninstalled-app-0123456789ab.png"
        stray.write_bytes(self.cand().png)
        self.assertEqual(sync(), [])
        self.assertFalse(stray.exists())
        self.assertTrue(os.path.exists(self.app(self.ID).icon))

    def test_reapplying_builds_on_the_updated_entry(self):
        self.applied_copy()
        old_icon = self.app(self.ID).icon
        self.system_entry(self.ID, entry("Example", icon="missing-icon", exec_line="/nonexistent/example --v2 %u"))
        apply(self.app(self.ID), self.cand(WIDE_SVG))
        now = self.app(self.ID)
        self.assertIn("--v2", now.exec)
        self.assertNotEqual(now.icon, old_icon)
        self.assertFalse(os.path.exists(old_icon))
        self.assertEqual(now.original_icon, "missing-icon")


class FakeWeb:
    """Answers IconFetch's requests from a dict: URL -> bytes, dict (JSON) or a callable(body)."""

    def __init__(self, routes):
        self.routes = routes
        self.asked = []

    def __call__(self, url, body=None, headers=None):
        self.asked.append(url)
        answer = self.routes.get(url)
        if answer is None:
            for prefix, value in self.routes.items():
                if prefix.endswith("*") and url.startswith(prefix[:-1]):
                    answer = value
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if callable(answer):
            answer = answer(body)
        return json.dumps(answer).encode() if isinstance(answer, (dict, list)) else answer


def flathub_search(hits):
    return lambda body: {"hits": hits.get(body["query"], [])}


class Matching(Sandbox):
    def test_flathub_matches_by_name_id_or_program_but_not_lookalikes(self):
        self.system_entry("firefox_firefox.desktop", entry("Firefox", exec_line="/snap/bin/firefox %u"))
        self.system_entry("vlc.desktop", entry("VLC media player", icon="vlc", exec_line="/usr/bin/vlc"))
        self.system_entry("info.desktop", entry("Info", icon="dialog-information", exec_line="info"))
        hits = {
            "Firefox": [{"app_id": "org.mozilla.firefox", "name": "Firefox"}, {"app_id": "com.flow.Flow", "name": "Flow"}],
            "VLC media player": [{"app_id": "org.videolan.VLC", "name": "VLC"}],
            "Info": [{"app_id": "com.kgurgul.cpuinfo", "name": "CPU Info"}, {"app_id": "org.example.Info", "name": "Info"}],
        }
        finder = Finder(FakeWeb({f"{FLATHUB}/search": flathub_search(hits)}))
        self.assertEqual(finder.flathub_ids(self.app("firefox_firefox.desktop")), ["org.mozilla.firefox"])
        self.assertEqual(finder.flathub_ids(self.app("vlc.desktop")), ["org.videolan.VLC"])
        # "Info" is an ordinary word: another app called Info isn't this one.
        self.assertEqual(finder.flathub_ids(self.app("info.desktop")), [])

    def test_flathub_falls_back_when_the_largest_icon_is_missing(self):
        self.system_entry("org.example.App.desktop", entry("Example", icon="org.example.App"))
        big, small = "https://dl/128x128@2/a.png", "https://dl/128x128/a.png"
        listing = {"icons": [{"url": small, "width": 128}, {"url": big, "width": 128, "scale": 2}], "icon": small}
        self.assertEqual(_icon_urls(listing), [big, small])
        web = FakeWeb({f"{FLATHUB}/search": {"hits": []}, f"{FLATHUB}/appstream/org.example.App": listing, small: SVG})
        found = Finder(web).flathub(self.app("org.example.App.desktop"), [])
        self.assertEqual([c.origin for c in found], ["org.example.App"])

    def test_snap_store_by_installed_snap_and_by_title(self):
        snapd = self.system / "snapd/applications"
        snapd.mkdir(parents=True)
        (snapd / "btop_btop.desktop").write_text(entry("btop++", exec_line="/snap/bin/btop"))
        os.environ["XDG_DATA_DIRS"] += f":{self.system / 'snapd'}"
        self.system_entry("figma-linux.desktop", entry("Figma", exec_line="/opt/figma/figma"))
        media = lambda url: {"media": [{"type": "banner", "url": "https://x/banner.png", "width": 720},
                                       {"type": "icon", "url": url, "width": 256}]}
        web = FakeWeb({
            f"{SNAP}/find*": lambda body: {"results": [{"name": "figma-linux", "snap": {"title": "Figma", **media("https://x/figma.svg")}},
                                                       {"name": "figjam", "snap": {"title": "FigJam", **media("https://x/jam.svg")}}]},
            f"{SNAP}/info/btop*": {"snap": {"website": "https://btop.dev", **media("https://x/btop.svg")}},
            "https://x/figma.svg": SVG, "https://x/btop.svg": SVG, "https://x/jam.svg": WIDE_SVG,
        })
        finder = Finder(web)
        homepages = []
        self.assertEqual([c.origin for c in finder.snap(self.app("btop_btop.desktop"), homepages)], ["btop"])
        self.assertEqual(homepages, ["https://btop.dev"])
        self.assertEqual([c.origin for c in finder.snap(self.app("figma-linux.desktop"), [])], ["figma-linux"])

    def test_brand_icons_by_name_after_dropping_packaging_words(self):
        self.system_entry("figma-linux-next.desktop", entry("figma-linux-next", icon="figma-linux-next"))
        self.system_entry("org.gnome.Nautilus.desktop", entry("Files", icon="org.gnome.Nautilus", exec_line="nautilus"))
        tree = {"svg": ["figma.svg", "files.svg"], "png": ["figma.png", "files.png"]}
        web = FakeWeb({f"{BRANDS}/tree.json": tree, f"{BRANDS}/svg/figma.svg": SVG, f"{BRANDS}/svg/files.svg": SVG})
        finder = Finder(web)
        self.assertEqual([c.origin for c in finder.brands(self.app("figma-linux-next.desktop"), [])], ["dashboard-icons/figma.svg"])
        # GNOME's Files is not the Windows app called Files.
        self.assertEqual(finder.brands(self.app("org.gnome.Nautilus.desktop"), []), [])

    def test_papirus_follows_symlinks_and_only_helps_apps_without_an_icon(self):
        self.theme_icon("scalable", "apps", "crisp")
        self.system_entry("colortune.desktop", entry("ColorTune", icon="preferences-color"))
        self.system_entry("crisp.desktop", entry("Crisp", icon="crisp"))
        web = FakeWeb({
            f"{PAPIRUS}/preferences-color.svg": b"preferences-desktop-color.svg",
            f"{PAPIRUS}/preferences-desktop-color.svg": SVG,
            f"{PAPIRUS}/crisp.svg": SVG,
        })
        finder = Finder(web)
        found = finder.papirus(self.app("colortune.desktop"), [])
        self.assertEqual([c.source for c in found], ["Papirus theme"])
        self.assertEqual(finder.papirus(self.app("crisp.desktop"), []), [])

    def test_website_icons_from_the_page_and_its_manifest(self):
        self.system_entry("app.desktop", entry("Example"))
        page = b"""<html><head>
            <link rel="icon" href="/favicon-32.png" sizes="32x32">
            <link rel="apple-touch-icon" href="/touch.png">
            <link rel="manifest" href="/site.webmanifest"></head></html>"""
        links = IconLinks("https://example.org/")
        links.feed(page.decode())
        self.assertEqual(links.manifest, "https://example.org/site.webmanifest")
        self.assertIn(("https://example.org/touch.png", 180), links.icons)
        web = FakeWeb({
            "https://example.org/": page,
            "https://example.org/site.webmanifest": {"icons": [{"src": "/icon-512.png", "sizes": "512x512"}]},
            "https://example.org/icon-512.png": to_png(SVG, 512)[0],
            "https://example.org/touch.png": to_png(SVG, 180)[0],
        })
        found = Finder(web).website(self.app("app.desktop"), ["https://example.org/"])
        self.assertEqual([(c.source, c.origin, c.size) for c in found], [("Website", "example.org", 512)])

    def test_website_skips_project_hosts(self):
        self.system_entry("app.desktop", entry("Example"))
        web = FakeWeb({})
        self.assertEqual(Finder(web).website(self.app("app.desktop"), ["https://github.com/example/app"]), [])
        self.assertEqual(web.asked, [])

    def test_an_unreachable_source_is_reported_and_the_rest_still_run(self):
        self.system_entry("org.example.App.desktop", entry("Example", icon="missing-thing"))

        def broken(url, body=None, headers=None):
            if "flathub" in url:
                raise urllib.error.URLError("no route to host")
            if url == f"{PAPIRUS}/missing-thing.svg":
                return SVG
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        found, problems = Finder(broken).candidates(self.app("org.example.App.desktop"))
        self.assertEqual([c.source for c in found], ["Papirus theme"])
        self.assertEqual(problems, ["Flathub: no route to host"])


class Downloads(Sandbox):
    """The download layer: one download per thing, kept on disk, over kept-alive connections."""

    def counting(self, answer=b"data", delay=0.0):
        calls = []

        def fetch(url, body=None, headers=None):
            calls.append(url)
            time.sleep(delay)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return fetch, calls

    def test_threads_asking_at_once_share_one_download(self):
        fetch, calls = self.counting(delay=0.2)
        web = Web(fetch)
        results = []
        threads = [threading.Thread(target=lambda: results.append(web.get("https://x/index.json"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [b"data"] * 8)
        self.assertEqual(len(calls), 1)

    def test_a_failure_is_shared_too_and_not_kept(self):
        fetch, calls = self.counting(answer=urllib.error.URLError("timed out"), delay=0.2)
        web = Web(fetch)
        errors = []

        def ask():
            try:
                web.get("https://x/slow")
            except OSError as error:
                errors.append(error)

        threads = [threading.Thread(target=ask) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(errors), len(calls)), (5, 1))
        fetch2, calls2 = self.counting()
        self.assertEqual(Web(fetch2).get("https://x/slow"), b"data")  # the next scan tries again

    def test_the_next_scan_reads_from_disk_and_look_again_does_not(self):
        fetch, calls = self.counting()
        Web(fetch).get("https://x/a.png")
        Web(fetch).get("https://x/a.png")
        self.assertEqual(len(calls), 1)
        Web(fetch, fresh=True).get("https://x/a.png")
        self.assertEqual(len(calls), 2)

    def test_old_downloads_expire_and_misses_expire_sooner(self):
        fetch, calls = self.counting()
        Web(fetch).get("https://x/a.png")
        for entry in sources.cache_dir().iterdir():
            os.utime(entry, (time.time() - 8 * 86400,) * 2)
        Web(fetch).get("https://x/a.png")
        self.assertEqual(len(calls), 2)

        missing, missed = self.counting(answer=urllib.error.HTTPError("https://x/b", 404, "Not Found", {}, None))
        self.assertIsNone(Web(missing).get("https://x/b"))
        self.assertIsNone(Web(missing).get("https://x/b"))
        self.assertEqual(len(missed), 1)
        for entry in sources.cache_dir().glob("*.missing"):
            os.utime(entry, (time.time() - 2 * 86400,) * 2)
        Web(missing).get("https://x/b")
        self.assertEqual(len(missed), 2)

    def test_the_website_is_only_asked_when_no_store_has_the_icon(self):
        self.system_entry("org.example.App.desktop", entry("Example", icon="org.example.App"))
        listing = {"icons": [{"url": "https://dl/a.png", "width": 128}], "urls": {"homepage": "https://example.org/"}}
        web = FakeWeb({f"{FLATHUB}/search": {"hits": []}, f"{FLATHUB}/appstream/org.example.App": listing, "https://dl/a.png": SVG})
        with Finder(web) as finder:
            found, _problems = finder.candidates(self.app("org.example.App.desktop"))
        self.assertEqual([c.source for c in found], ["Flathub"])
        self.assertNotIn("https://example.org/", web.asked)

        self.system_entry("other.desktop", entry("Other Thing", exec_line="/nonexistent/other"))
        web = FakeWeb({})
        with Finder(web) as finder:
            finder.candidates(self.app("other.desktop"))
        self.assertTrue(any(url.startswith(SNAP) for url in web.asked))


class KeptAlive(unittest.TestCase):
    """The real HTTP client, against a local server."""

    def setUp(self):
        self.connections = 0
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                test.connections += 1
                super().setup()

            def reply(self, code, body=b"", headers=()):
                self.send_response(code)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/moved":
                    self.reply(301, headers=[("Location", "/here")])
                elif self.path == "/here":
                    self.reply(200, b"arrived")
                elif self.path.startswith("/n/"):
                    self.reply(200, self.path.encode())
                elif self.path == "/then-hang-up":
                    self.reply(200, b"bye")
                    self.close_connection = True  # without saying so, as an idle timeout does
                else:
                    self.reply(404, b"no")

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                self.reply(200, self.rfile.read(length))

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.saved = {var: os.environ.pop(var) for var in sources.PROXY_VARS if var in os.environ}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.update(self.saved)
        sources._local.__dict__.pop("connections", None)

    def test_one_connection_serves_many_requests(self):
        for n in range(5):
            self.assertEqual(sources._request(f"{self.base}/n/{n}"), f"/n/{n}".encode())
        self.assertEqual(self.connections, 1)

    def test_redirects_errors_and_posts(self):
        self.assertEqual(sources._request(f"{self.base}/moved"), b"arrived")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            sources._request(f"{self.base}/nothing")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.loads(sources._request(f"{self.base}/search", {"query": "vlc"})), {"query": "vlc"})

    def test_a_connection_the_server_closed_is_reopened(self):
        self.assertEqual(sources._request(f"{self.base}/then-hang-up"), b"bye")
        time.sleep(0.1)
        self.assertEqual(sources._request(f"{self.base}/n/2"), b"/n/2")
        self.assertEqual(self.connections, 2)


class AppFolder(Sandbox):
    def test_finds_icons_next_to_an_unpacked_program(self):
        folder = Path(self.tmp.name) / "Downloads/zen"
        (folder / "browser/chrome/icons/default").mkdir(parents=True)
        (folder / "zen").write_text("#!/bin/sh\n")
        (folder / "browser/chrome/icons/default/default128.png").write_bytes(to_png(SVG, 128)[0])
        (folder / "browser/chrome/icons/default/default16.png").write_bytes(to_png(SVG, 16)[0])
        self.system_entry("zen.desktop", entry("Zen", exec_line=f"{folder}/zen %u"))
        found = folder_candidates(self.app("zen.desktop"))
        self.assertEqual([Path(c.origin).name for c in found], ["default128.png"])

    def test_ignores_shared_system_folders(self):
        self.system_entry("vim.desktop", entry("Vim", exec_line="/usr/bin/vim"))
        self.assertEqual(folder_candidates(self.app("vim.desktop")), [])


if __name__ == "__main__":
    unittest.main()


def svg(body, width=64, height=64):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">{body}</svg>'.encode()


# A tile already, its corners rounder than the shared shape's.
ROUND_TILE = svg('<rect width="64" height="64" rx="24" fill="#2a5bd7"/><circle cx="32" cy="32" r="10" fill="#fff"/>')
# A badge of one colour.
BADGE = svg('<circle cx="32" cy="32" r="32" fill="#e0103a"/><path d="M26 20 L46 32 L26 44 Z" fill="#fff"/>')
# A badge whose edge changes colour round it: a logo, not a tile.
PIE = svg('<path d="M32 32 L32 0 A32 32 0 0 1 64 32 Z" fill="#e33"/><path d="M32 32 L64 32 A32 32 0 0 1 32 64 Z" fill="#3a3"/>'
          '<path d="M32 32 L32 64 A32 32 0 0 1 0 32 Z" fill="#33e"/><path d="M32 32 L0 32 A32 32 0 0 1 32 0 Z" fill="#ec3"/>')
TRIANGLE = svg('<path d="M32 4 L60 58 L4 58 Z" fill="#f07818"/>')
BANNER = svg('<rect width="64" height="32" fill="#0a8"/>', 64, 32)
WHITE_STAR = svg('<path d="M32 4 L40 24 L62 24 L44 38 L50 60 L32 46 L14 60 L20 38 L2 24 L24 24 Z" fill="#fafafa"/>')


class Shapes(unittest.TestCase):
    def shape(self, data):
        return shapes.analyze(sources.decode(data, shapes.DRAWN)[0])

    def pixel(self, pixbuf, x, y):
        data, i = pixbuf.get_pixels(), y * pixbuf.get_rowstride() + x * 4
        return tuple(data[i:i + 4])

    def render(self, data, **kw):
        with tempfile.NamedTemporaryFile(suffix=".svg") as file:
            file.write(data)
            file.flush()
            return shapes.render(file.name, **kw)

    def test_tiles_are_told_from_other_shapes(self):
        self.assertEqual(self.shape(ROUND_TILE).kind, shapes.FILL)
        self.assertEqual(self.shape(BADGE).kind, shapes.FILL)
        self.assertEqual(self.shape(PIE).kind, shapes.SET)
        self.assertEqual(self.shape(TRIANGLE).kind, shapes.SET)
        self.assertEqual(self.shape(BANNER).kind, shapes.SET)

    def test_a_tile_is_cut_to_the_shape_and_its_corners_filled(self):
        pic = self.render(ROUND_TILE)
        self.assertEqual((pic.get_width(), pic.get_height()), (shapes.SIZE, shapes.SIZE))
        self.assertEqual(self.pixel(pic, 0, 0)[3], 0)
        o = shapes.outline()
        # Inside the shared outline where the original's rounder corner left a gap: filled in its blue.
        r, g, b, a = self.pixel(pic, o.offset + round(o.side * 0.06), o.offset + round(o.side * 0.12))
        self.assertEqual(a, 255)
        self.assertTrue(b > 180 and r < 80, (r, g, b))
        self.assertEqual(self.pixel(pic, shapes.SIZE // 2, shapes.SIZE // 2)[:3], (255, 255, 255))

    def test_another_shape_is_set_on_a_tile_in_its_own_colour(self):
        pic = self.render(TRIANGLE)
        o = shapes.outline()
        r, g, b, a = self.pixel(pic, o.offset + round(o.side * 0.1), o.offset + round(o.side * 0.5))
        self.assertEqual(a, 255)
        self.assertTrue(r > g > b > 200, (r, g, b))  # a pale wash of the orange
        r, g, b, _ = self.pixel(self.render(TRIANGLE, plain=True), o.offset + round(o.side * 0.1), o.offset + round(o.side * 0.5))
        self.assertTrue(max(r, g, b) - min(r, g, b) < 8, (r, g, b))  # plain: grey
        # A white picture would vanish on a pale tile: it gets a dark one.
        r, g, b, _ = self.pixel(self.render(WHITE_STAR), o.offset + round(o.side * 0.1), o.offset + round(o.side * 0.1))
        self.assertLess(max(r, g, b), 100)

    def test_the_shadow_falls_below_the_tile(self):
        pic, o = self.render(TRIANGLE), shapes.outline()
        middle = shapes.SIZE // 2
        below = self.pixel(pic, middle, o.offset + o.side + 2)[3]
        above = self.pixel(pic, middle, o.offset - 3)[3]
        self.assertGreater(below, above)


class SameShapePack(Sandbox):
    def setUp(self):
        super().setUp()
        self.config = Path(self.tmp.name) / "config"
        self.env["XDG_CONFIG_HOME"] = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.config)
        self.ada_icons = Path(self.tmp.name) / "ada-icons"
        self.ada_icons.mkdir()
        self.real_ada_icons, shapes.ADA_ICONS = shapes.ADA_ICONS, self.ada_icons
        self.picture(TRIANGLE, "triangle.svg")
        self.system_entry("org.example.Tri.desktop", entry("Triangle", icon=str(self.home / "triangle.svg")))
        self.system_entry("no-icon.desktop", entry("Nothing"))
        (self.ada_icons / "ada-thing.svg").write_bytes(ROUND_TILE)
        self.system_entry("ada-thing.desktop", entry("Ada Thing", icon="ada-thing"))
        self.long_id = "ada-windows-" + "9e44" * 16 + "-" + "b608" * 16
        self.user_entry(f"{self.long_id}.desktop", entry("Windows App", icon=str(self.home / "triangle.svg")))

    def tearDown(self):
        shapes.ADA_ICONS = self.real_ada_icons
        super().tearDown()

    def picture(self, data, name):
        (self.home / name).write_bytes(data)

    def look(self, icons=None):
        (self.config / "ada").mkdir(parents=True, exist_ok=True)
        (self.config / "ada/look.ini").write_text("[Look]\nActive=mine\n")
        folder = self.home / "ada/looks/mine"
        folder.mkdir(parents=True, exist_ok=True)
        look = {"name": "Mine", "style": {"corners": 1.04}}
        if icons is not None:
            look["icons"] = icons
        (folder / "look.json").write_text(json.dumps(look, indent=4, sort_keys=True) + "\n")
        return folder / "look.json"

    def test_every_app_gets_a_picture_named_by_its_id_in_adas_packs(self):
        report = shapes.build()
        pack = self.home / "ada/icon-packs" / shapes.PACK_ID
        self.assertEqual(report.folder, pack)
        self.assertEqual(json.loads((pack / "pack.json").read_text())["name"], "Same shape")
        made = sorted(p.name for p in (pack / "icons").iterdir())
        self.assertEqual(made, sorted(["org.example.Tri.png", "ada-thing.png", f"{self.long_id}.png"]))
        self.assertEqual(report.left_out, [("Nothing", "no icon")])

    def test_a_refresh_keeps_what_hasnt_changed_and_follows_what_has(self):
        shapes.build()
        pack = self.home / "ada/icon-packs" / shapes.PACK_ID / "icons"
        report = shapes.refresh()
        self.assertEqual(report.made, [])
        self.assertEqual(len(report.kept), 3)

        self.picture(BADGE, "triangle.svg")  # the app's icon changed
        os.utime(self.home / "triangle.svg", ns=(1, 1))
        (self.system / "applications/ada-thing.desktop").unlink()  # uninstalled
        report = shapes.refresh()
        self.assertEqual(sorted(report.made), ["Triangle", "Windows App"])
        self.assertFalse((pack / "ada-thing.png").exists())

    def test_without_a_pack_refresh_does_nothing(self):
        self.assertIsNone(shapes.refresh())

    def test_wearing_and_taking_off_puts_back_what_the_look_wore(self):
        before = {"fallback": "tint", "pack": "other"}
        path = self.look(before)
        original = path.read_text()
        shapes.build()
        shapes.wear()
        self.assertEqual(json.loads(path.read_text())["icons"], shapes.PACK_ID)
        self.assertTrue(shapes.worn())
        shapes.wear()  # twice: still remembers the first
        shapes.take_off()
        self.assertFalse(shapes.worn())
        self.assertEqual(path.read_text(), original)

        path = self.look()
        shapes.wear()
        shapes.take_off()
        self.assertNotIn("icons", json.loads(path.read_text()))

    def test_a_built_in_look_is_left_to_settings(self):
        (self.config / "ada").mkdir(parents=True)
        (self.config / "ada/look.ini").write_text("[Look]\nActive=ada-default\n")
        with self.assertRaises(ValueError):
            shapes.wear()
        self.assertFalse(shapes.worn())

    def test_the_command_wears_it_on_ada_and_only_writes_a_folder_elsewhere(self):
        path = self.look()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["shape"]), 0)
            self.assertEqual(json.loads(path.read_text())["icons"], shapes.PACK_ID)
            self.assertEqual(main(["shape", "--off"]), 0)
            self.assertNotIn("icons", json.loads(path.read_text()))

            os.environ["XDG_CURRENT_DESKTOP"] = "GNOME"
            self.assertEqual(main(["shape"]), 0)
        self.assertNotIn("icons", json.loads(path.read_text()))
        self.assertTrue((self.home / "iconfetch/same-shape/icons/org.example.Tri.png").is_file())

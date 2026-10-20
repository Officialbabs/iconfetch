"""IconFetch tests. Every test runs in its own XDG data folders: nothing touches the real desktop."""

import base64
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

from iconfetch import apps as apps_mod
from iconfetch.apps import GENERIC, LOWRES, MISSING, OWN, IconIndex, exec_program, installed_apps
from iconfetch.install import REMOVED, RESTORED, UPDATED, apply, icons_dir, revert, suggested, sync
from iconfetch.sources import BRANDS, FLATHUB, PAPIRUS, SNAP, Candidate, Finder, IconLinks, _icon_urls, folder_candidates, to_png

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#f60"/></svg>'
WIDE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#06f"/></svg>'
# 1×1 transparent PNG
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


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
        # GdkPixbuf's glycin loaders (SVG among them) and the MIME database they
        # sniff with are found through XDG_DATA_DIRS.
        loaders = self.loaders = root / "loaders"
        loaders.mkdir()
        for name in ("glycin-loaders", "mime"):
            if Path("/usr/share", name).is_dir():
                (loaders / name).symlink_to(Path("/usr/share", name))
        self.env = {k: os.environ.get(k) for k in ("XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CURRENT_DESKTOP")}
        os.environ["XDG_DATA_HOME"] = str(self.home)
        os.environ["XDG_DATA_DIRS"] = f"{self.system}:{loaders}"
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
        os.environ["XDG_DATA_DIRS"] = str(self.loaders)
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

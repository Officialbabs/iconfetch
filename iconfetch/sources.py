"""Where an app's real icon comes from.

Official sources come first: Flathub and the Snap Store (the icons app authors
publish there), the app's own folder, and the app's website. Then two
collections: dashboard-icons (brand icons by name), and the Papirus theme,
which still has full-colour icons for the system tools whose generic icon
names Adwaita dropped.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

from . import __version__
from .apps import GENERIC, GENERIC_NAMES, MISSING, App, data_dirs, exec_program, norm

FLATHUB = "https://flathub.org/api/v2"
SNAP = "https://api.snapcraft.io/v2/snaps"
SNAP_HEADERS = {"Snap-Device-Series": "16"}
BRANDS = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"
PAPIRUS = "https://raw.githubusercontent.com/PapirusDevelopmentTeam/papirus-icon-theme/master/Papirus/64x64/apps"
# Some sites turn away clients that don't look like a browser.
USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) IconFetch/{__version__}"
TIMEOUT = 12
SIZE = 256  # pixels: what IconFetch stores and applies
MAX_DOWNLOAD = 4 * 1024 * 1024
MIN_WEB_ICON = 96  # a site's favicon below this is too small for a dock

# A homepage on these is a project page or a store: its icon is the host's, not the app's.
SHARED_HOSTS = (
    "github.com", "gitlab.com", "codeberg.org", "sourceforge.net", "launchpad.net", "gnome.org",
    "kde.org", "freedesktop.org", "wikipedia.org", "snapcraft.io", "flathub.org", "google.com",
)
# Programs that live in shared system folders: their folder isn't the app's.
SHARED_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/snap/", "/opt/homebrew/", "/nix/", "/run/")
FOLDER_DEPTH = 6
FOLDER_FILES = 20000
FOLDER_NAME = re.compile(r"^(default|icon|logo|app|appicon|product)[-_]?\d*$", re.I)


@dataclass
class Candidate:
    source: str  # shown to the person: "Flathub", "Website"…
    origin: str  # the store's app ID, the site, the file
    png: bytes  # SIZE×SIZE PNG (smaller when the original is smaller)
    size: int  # the original's short side; 0 for a vector

    @property
    def quality(self) -> str:
        return "vector" if self.size == 0 else f"{self.size}px"

    @property
    def text(self) -> str:
        return f"{self.source} · {self.quality}"


# --- Images -----------------------------------------------------------------


def to_png(data: bytes, size: int = SIZE) -> tuple[bytes, int]:
    """Decode any icon format GdkPixbuf reads (PNG, SVG, ICO…) into a square PNG of at most `size`."""
    if not data:
        raise ValueError("empty")
    loader = GdkPixbuf.PixbufLoader()
    native = {}

    def prepared(ldr, width, height):
        fmt = ldr.get_format()
        vector = bool(fmt and fmt.is_scalable())
        native["size"] = 0 if vector else min(width, height)
        longest = max(width, height)
        if vector or longest > size:
            scale = size / longest
            ldr.set_size(max(1, round(width * scale)), max(1, round(height * scale)))

    loader.connect("size-prepared", prepared)
    try:
        loader.write(data)
        loader.close()
    except GLib.Error as error:
        raise ValueError(f"not an image: {error.message}") from None
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ValueError("not an image")
    pixbuf = _square(pixbuf.apply_embedded_orientation() or pixbuf)
    ok, png = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise ValueError("could not encode PNG")
    return bytes(png), native.get("size", min(pixbuf.get_width(), pixbuf.get_height()))


def _square(pixbuf: GdkPixbuf.Pixbuf) -> GdkPixbuf.Pixbuf:
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    if width == height:
        return pixbuf
    side = max(width, height)
    canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, side, side)
    canvas.fill(0x00000000)
    pixbuf.copy_area(0, 0, width, height, canvas, (side - width) // 2, (side - height) // 2)
    return canvas


# --- Network ----------------------------------------------------------------

Fetch = Callable[..., bytes]  # (url, body=None, headers=None) -> bytes


def _request(url: str, body: Optional[dict] = None, headers: Optional[dict] = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = response.read(MAX_DOWNLOAD + 1)
    if len(payload) > MAX_DOWNLOAD:
        raise ValueError(f"{url} is larger than {MAX_DOWNLOAD} bytes")
    return payload


class Web:
    """Downloads, kept for the length of one scan. Not found (any 4xx but 429) is None."""

    def __init__(self, fetch: Fetch = _request) -> None:
        self.fetch = fetch
        self._cache: dict[tuple, Optional[bytes]] = {}
        self._lock = threading.Lock()

    def get(self, url: str, body: Optional[dict] = None, headers: Optional[dict] = None) -> Optional[bytes]:
        key = (url, json.dumps(body, sort_keys=True) if body is not None else None)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            data = self.fetch(url, body, headers)
        except urllib.error.HTTPError as error:
            error.close()
            if not 400 <= error.code < 500 or error.code == 429:
                raise
            data = None
        with self._lock:
            self._cache[key] = data
        return data

    def json(self, url: str, body: Optional[dict] = None, headers: Optional[dict] = None):
        data = self.get(url, body, headers)
        try:
            return json.loads(data) if data else None
        except ValueError:
            return None

    def image(self, url: str) -> Optional[tuple[bytes, int]]:
        """The picture at `url` as IconFetch's PNG, or None if it isn't one."""
        try:
            return to_png(self.get(url))
        except (OSError, ValueError):
            return None


# --- Finding ----------------------------------------------------------------


class Finder:
    """Looks for an app's icon in every source. One per scan: it keeps what it downloaded."""

    def __init__(self, fetch: Fetch = _request) -> None:
        self.web = Web(fetch)
        self._homepages: Optional[dict[str, str]] = None
        self._lock = threading.Lock()
        self.sources = (
            ("Flathub", self.flathub),
            ("Snap Store", self.snap),
            ("App folder", self.folder),
            ("Website", self.website),
            ("Brand icons", self.brands),
            ("Papirus", self.papirus),
        )

    def candidates(self, app: App) -> tuple[list[Candidate], list[str]]:
        """Every icon found for the app, most official first, and the sources that couldn't be reached."""
        found: list[Candidate] = []
        problems = []
        homepages: list[str] = []
        for name, source in self.sources:
            try:
                found += source(app, homepages)
            except (OSError, ValueError) as error:
                problems.append(f"{name}: {getattr(error, 'reason', None) or error}")
        unique: dict[bytes, Candidate] = {}
        for cand in found:
            unique.setdefault(cand.png, cand)
        return list(unique.values()), problems

    # Flathub: the AppStream listing, with the icon its developers ship.

    def flathub_ids(self, app: App) -> list[str]:
        """Flathub app IDs that are this app: its own reverse-DNS ID, or a search hit with the same name."""
        ids = []
        icon = app.original_icon if app.applied else app.icon
        for own in (app.stem, icon):
            if own and own.count(".") >= 2 and "/" not in own:
                ids.append(own)
        keys = app.match_keys()
        found = self.web.json(f"{FLATHUB}/search", {"query": app.name}) or {}
        for hit in (found.get("hits") or [])[:12] if isinstance(found, dict) else []:
            app_id = hit.get("app_id") or ""
            last = app_id.rsplit(".", 1)[-1]
            if norm(hit.get("name", "")) in keys or norm(last) in keys or norm(app_id) in keys:
                ids.append(app_id)
        return list(dict.fromkeys(i for i in ids if i))

    def flathub(self, app: App, homepages: list[str]) -> list[Candidate]:
        found = []
        for app_id in self.flathub_ids(app):
            listing = self.web.json(f"{FLATHUB}/appstream/{urllib.parse.quote(app_id)}")
            if not isinstance(listing, dict):
                continue
            homepages.append((listing.get("urls") or {}).get("homepage") or "")
            # Listings name sizes Flathub doesn't always have (a 404 on @2): try the next one down.
            for url in _icon_urls(listing):
                got = self.web.image(url)
                if got:
                    found.append(Candidate("Flathub", app_id, *got))
                    break
        return found

    # Snap Store: the icon a snap's publisher uploaded.

    def snap(self, app: App, homepages: list[str]) -> list[Candidate]:
        snaps: dict[str, Optional[dict]] = {}
        if "snapd" in app.path.parts:
            snaps[app.stem.split("_", 1)[0]] = None
        query = urllib.parse.urlencode({"q": app.name, "fields": "title,media,website"})
        results = (self.web.json(f"{SNAP}/find?{query}", headers=SNAP_HEADERS) or {}).get("results") or []
        keys = app.match_keys()
        for result in results[:10]:
            info, name = result.get("snap") or {}, result.get("name") or ""
            if name and (norm(info.get("title", "")) in keys or norm(name) in keys):
                snaps[name] = info
        found = []
        for name, info in snaps.items():
            if info is None:
                url = f"{SNAP}/info/{urllib.parse.quote(name)}?fields=title,media,website"
                info = (self.web.json(url, headers=SNAP_HEADERS) or {}).get("snap") or {}
            homepages.append(info.get("website") or "")
            icons = [m for m in info.get("media") or [] if m.get("type") == "icon" and m.get("url")]
            for media in sorted(icons, key=lambda m: m.get("width") or 0, reverse=True):
                got = self.web.image(media["url"])
                if got:
                    found.append(Candidate("Snap Store", name, *got))
                    break
        return found

    # The app's own folder.

    def folder(self, app: App, _homepages: list[str]) -> list[Candidate]:
        return folder_candidates(app)

    # The app's website: its touch icon, manifest icons, or a large favicon.

    def website(self, app: App, homepages: list[str]) -> list[Candidate]:
        local = self.local_homepages().get(app.id)
        for page in dict.fromkeys(p for p in [*homepages, local] if p):
            host = urllib.parse.urlsplit(page).hostname or ""
            if not host or any(host == h or host.endswith("." + h) for h in SHARED_HOSTS):
                continue
            cand = self._site_icon(page, host)
            if cand:
                return [cand]
        return []

    def _site_icon(self, page: str, host: str) -> Optional[Candidate]:
        try:
            html = self.web.get(page, headers={"Accept": "text/html"})
        except (OSError, ValueError):
            return None
        links = IconLinks(page)
        if html:
            links.feed(html[:1_000_000].decode("utf-8", "replace"))
        options = list(links.icons)
        if links.manifest:
            try:
                manifest = self.web.json(links.manifest)
            except (OSError, ValueError):
                manifest = None
            for icon in (manifest or {}).get("icons") or [] if isinstance(manifest, dict) else []:
                if icon.get("src"):
                    options.append((urllib.parse.urljoin(links.manifest, icon["src"]), _declared_size(icon.get("sizes"), icon["src"])))
        options.append((urllib.parse.urljoin(page, "/apple-touch-icon.png"), 180))
        for url, _declared in sorted(dict.fromkeys(options), key=lambda o: o[1], reverse=True):
            try:
                got = self.web.image(url)
            except (OSError, ValueError):
                got = None
            if got and (got[1] == 0 or got[1] >= MIN_WEB_ICON):
                return Candidate("Website", host, *got)
        return None

    def local_homepages(self) -> dict[str, str]:
        """Homepages from installed AppStream files, by desktop ID."""
        with self._lock:
            if self._homepages is None:
                self._homepages = _read_metainfo()
            return self._homepages

    # dashboard-icons: a collection of brand app icons, named by app.

    def brands(self, app: App, _homepages: list[str]) -> list[Candidate]:
        tree = self.web.json(f"{BRANDS}/tree.json")
        if not isinstance(tree, dict):
            return []
        files = {}
        for kind in ("svg", "png"):
            for name in tree.get(kind) or []:
                files.setdefault(norm(Path(name).stem), (kind, name))
        keys = app.match_keys()
        ordered = [norm(app.name)] + sorted(keys)
        for key in ordered:
            if key in keys and key in files and key not in GENERIC_NAMES:
                kind, name = files[key]
                got = self.web.image(f"{BRANDS}/{kind}/{urllib.parse.quote(name)}")
                if got:
                    return [Candidate("Brand icons", f"dashboard-icons/{name}", *got)]
        return []

    # Papirus: full-colour theme icons, for apps that show a stand-in now.

    def papirus(self, app: App, _homepages: list[str]) -> list[Candidate]:
        if app.state.status not in (MISSING, GENERIC):
            return []
        icon = app.original_icon if app.applied else app.icon
        names = [icon] if icon and "/" not in icon else []
        names += [app.stem, Path(exec_program(app.exec)).name]
        for name in dict.fromkeys(n for n in names if n):
            data = self._papirus_file(name)
            if data:
                try:
                    return [Candidate("Papirus theme", f"papirus/{name}", *to_png(data))]
                except ValueError:
                    continue
        return []

    def _papirus_file(self, name: str) -> Optional[bytes]:
        url = f"{PAPIRUS}/{urllib.parse.quote(name)}.svg"
        data = self.web.get(url)
        # GitHub serves a symlink as its target's path: follow it a few hops.
        for _ in range(3):
            if not data or len(data) > 512 or b"<" in data:
                break
            url = urllib.parse.urljoin(url, data.decode("utf-8", "replace").strip())
            data = self.web.get(url)
        return data if data and b"<" in data else None


def _icon_urls(listing: dict) -> list[str]:
    """A Flathub listing's icons, largest first."""
    icons = [i for i in listing.get("icons") or [] if i.get("url")]
    icons.sort(key=lambda i: (i.get("width") or 0) * (i.get("scale") or 1), reverse=True)
    urls = [i["url"] for i in icons]
    if listing.get("icon"):
        urls.append(listing["icon"])
    return list(dict.fromkeys(urls))


def _declared_size(sizes: Optional[str], href: str = "") -> int:
    """The largest size a <link sizes> or manifest entry declares; vectors and `any` rank first."""
    if href.lower().split("?")[0].endswith(".svg") or (sizes or "").strip().lower() == "any":
        return 4096
    found = [int(w) for w, _h in re.findall(r"(\d+)x(\d+)", sizes or "")]
    return max(found, default=0)


class IconLinks(HTMLParser):
    """The icons a page names in its <head>: touch icons, sized icons, and the web app manifest."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self.page = page
        self.icons: list[tuple[str, int]] = []
        self.manifest: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attr = {k: v or "" for k, v in attrs}
        rel = set(attr.get("rel", "").lower().split())
        href = attr.get("href")
        if not href:
            return
        url = urllib.parse.urljoin(self.page, href)
        if "manifest" in rel:
            self.manifest = url
        elif rel & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
            self.icons.append((url, _declared_size(attr.get("sizes"), href) or 180))
        elif "icon" in rel:
            self.icons.append((url, _declared_size(attr.get("sizes"), href)))


def _read_metainfo() -> dict[str, str]:
    homepages = {}
    for base in data_dirs():
        for folder in ("metainfo", "appdata"):
            root = base / folder
            if not root.is_dir():
                continue
            for path in root.glob("*.xml"):
                try:
                    component = ElementTree.parse(path).getroot()
                except (ElementTree.ParseError, OSError):
                    continue
                url = next((u.text for u in component.iter("url") if u.get("type") == "homepage" and u.text), None)
                if not url:
                    continue
                ids = [l.text.strip() for l in component.iter("launchable") if l.get("type") == "desktop-id" and l.text]
                own = component.findtext("id")
                if own:
                    own = own.strip()
                    ids.append(own if own.endswith(".desktop") else own + ".desktop")
                for desktop_id in ids:
                    homepages.setdefault(desktop_id, url.strip())
    return homepages


# --- The app's own folder ---------------------------------------------------


def folder_candidates(app: App) -> list[Candidate]:
    """Icons shipped next to a program that lives in its own folder (an unpacked download)."""
    program = exec_program(app.exec)
    if not program.startswith("/"):
        return []
    real = os.path.realpath(program)
    root = Path(real).parent
    if real.startswith(SHARED_PREFIXES) or root in (Path.home(), Path.home() / ".local/bin", Path.home() / "bin"):
        return []
    if not root.is_dir() or real.endswith(".AppImage"):
        return []
    keys = app.match_keys()
    files = []
    seen = 0
    for dirpath, dirs, names in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= FOLDER_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "locale", "locales")]
        for name in names:
            seen += 1
            stem, ext = os.path.splitext(name)
            if ext.lower() in (".png", ".svg") and (FOLDER_NAME.match(stem) or norm(stem) in keys):
                files.append(os.path.join(dirpath, name))
        if seen > FOLDER_FILES:
            break
    found = []
    for path in files:
        try:
            with open(path, "rb") as handle:
                png, size = to_png(handle.read(MAX_DOWNLOAD))
        except (OSError, ValueError):
            continue
        if size == 0 or size >= 48:
            found.append(Candidate("App folder", path, png, size))
    found.sort(key=lambda c: (c.size == 0, c.size), reverse=True)
    return found[:2]

"""One shape for every icon: each app's own icon cut to one rounded tile, or set on one, as an Ada icon pack.

An icon that already is a tile (a rounded square, or a circle of one colour)
is cut to the shape, its corners filled in its own edge colour. Any other
shape (a triangle, a gear, a folder) is set on a tile washed in its own
colour. The pictures stay the apps' own: only the outline is shared.
"""

from __future__ import annotations

import configparser
import functools
import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

from .apps import ADA_OWN, App, config_home, data_home, installed_apps, on_ada, store_dir
from .sources import decode

SIZE = 256  # pixels of each icon in the pack
TILE = 0.88  # the tile's width, of the icon's; the rest holds its shadow
ROUNDNESS = 5.0  # the outline |x|^n + |y|^n = 1: 2 is a circle, higher is squarer
DRAWN = 512  # pixels a source is drawn at before it's fitted to the tile
LOOKED = 128  # pixels a source is looked at to tell its shape
SHADOW = 0.22  # the shadow's darkness under the tile
STYLE = 1  # bump when the drawing changes: every icon is made again

FILL, SET = "fill", "set"
# Ada's own apps take their pictures from the desktop's own folder, by icon name.
ADA_ICONS = Path("/usr/share/ada/qml/icons")
PACK_ID = "iconfetch-same-shape"
PACK = {
    "name": "Same shape",
    "about": "Every app's own icon on one rounded tile. Made by IconFetch, which keeps it up to date.",
    "author": "IconFetch",
}

WHITE, BLACK = (255, 255, 255), (0, 0, 0)
NEUTRAL_LIGHT = ((251, 251, 253), (229, 229, 234))
NEUTRAL_DARK = ((74, 74, 78), (40, 40, 43))


@dataclass
class Shape:
    kind: str  # FILL: already a tile, cut to the shape; SET: set on a tile
    box: tuple[float, float, float, float]  # the solid part: x, y, width, height, as fractions of the picture
    coverage: float  # how much of that box is solid
    corners: tuple[tuple[int, int, int], ...]  # edge colour near each corner: top left, top right, bottom left, bottom right
    ink: Optional[tuple[int, int, int]]  # its most vivid colour; None for a grey picture
    light: float  # how light the solid part is, 0 to 1


# --- Looking -------------------------------------------------------------------


def _luma(rgb) -> float:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def _mix(a, b, t: float) -> tuple[int, int, int]:
    """a, moved t of the way to b."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def analyze(pixbuf: GdkPixbuf.Pixbuf) -> Shape:
    """Tell a square picture's shape: whether it already is a tile, where its solid part is, its colours."""
    side = pixbuf.get_width()
    if side != LOOKED:
        pixbuf = pixbuf.scale_simple(LOOKED, LOOKED, GdkPixbuf.InterpType.BILINEAR)
        side = LOOKED
    data, stride = pixbuf.get_pixels(), pixbuf.get_rowstride()
    alpha = [[data[y * stride + x * 4 + 3] for x in range(side)] for y in range(side)]
    rows = [y for y in range(side) if max(alpha[y]) >= 128]
    cols = [x for x in range(side) if any(alpha[y][x] >= 128 for y in rows)]
    if not rows:
        raise ValueError("the picture is empty")
    x0, x1, y0, y1 = cols[0], cols[-1] + 1, rows[0], rows[-1] + 1
    width, height = x1 - x0, y1 - y0
    solid = sum(1 for y in range(y0, y1) for x in range(x0, x1) if alpha[y][x] >= 128)
    coverage = solid / (width * height)

    # The edge, just inside: solid pixels a few steps from the outside.
    step = max(2, side // 40)

    def outside(x, y):
        return not (x0 <= x < x1 and y0 <= y < y1) or alpha[y][x] < 128

    quarters = [[], [], [], []]
    vivid = [0.0, 0.0, 0.0, 0.0]  # weighted r, g, b and the weight
    light = weight = 0.0
    for y in range(y0, y1):
        for x in range(x0, x1):
            a = alpha[y][x]
            if a < 128:
                continue
            i = y * stride + x * 4
            rgb = (data[i], data[i + 1], data[i + 2])
            share = a / 255
            light += _luma(rgb) * share
            weight += share
            high, low = max(rgb), min(rgb)
            saturation = (high - low) / high if high else 0.0
            w = share * saturation * saturation * (high / 255)
            for k in range(3):
                vivid[k] += rgb[k] * w
            vivid[3] += w
            if a >= 240 and (outside(x - step, y) or outside(x + step, y) or outside(x, y - step) or outside(x, y + step)):
                quarters[(y >= (y0 + y1) / 2) * 2 + (x >= (x0 + x1) / 2)].append(rgb)

    everything = [rgb for quarter in quarters for rgb in quarter]
    corners, steady = [], True
    for quarter in quarters:
        quarter = quarter or everything or [WHITE]
        mean = tuple(round(sum(c[k] for c in quarter) / len(quarter)) for k in range(3))
        near = sum(1 for c in quarter if max(abs(c[k] - mean[k]) for k in range(3)) <= 36)
        steady = steady and near >= 0.85 * len(quarter)
        corners.append(mean)
    # One colour all round, or one shading gently into another: not a logo of many colours.
    steady = steady and all(max(abs(a[k] - b[k]) for k in range(3)) <= 60 for a in corners for b in corners)
    ink = tuple(round(vivid[k] / vivid[3]) for k in range(3)) if vivid[3] > 0.04 * weight else None

    squarish = 0.9 <= width / height <= 1 / 0.9
    # A rounded square (about 0.95) is a tile already; so is a round badge of one colour (about 0.79).
    tile = squarish and (coverage >= 0.86 or (coverage >= 0.74 and steady))
    box = (x0 / side, y0 / side, width / side, height / side)
    return Shape(FILL if tile else SET, box, coverage, tuple(corners), ink, light / weight if weight else 1.0)


# --- Drawing -------------------------------------------------------------------


@dataclass
class Outline:
    side: int  # the tile's width in the icon
    offset: int  # its distance from the icon's edges
    spans: list[tuple[int, int]]  # each row's run of pixels wholly inside the tile (empty: none)
    edges: list[tuple[int, int, float, float]]  # every other pixel that shows: x, y, tile cover, opacity with shadow


@functools.lru_cache(maxsize=4)
def outline(size: int = SIZE, tile: float = TILE) -> Outline:
    """Where the tile and its shadow fall in a `size` icon."""
    side = round(size * tile)
    offset = (size - side) // 2
    half = side / 2
    middle = offset + half

    def inside(px: float, py: float) -> bool:
        return abs((px - middle) / half) ** ROUNDNESS + abs((py - middle) / half) ** ROUNDNESS <= 1

    cover = [0.0] * (size * size)
    samples = [(i + 0.5) / 4 for i in range(4)]
    for y in range(offset - 1, offset + side + 1):
        for x in range(offset - 1, offset + side + 1):
            g = abs((x + 0.5 - middle) / half) ** ROUNDNESS + abs((y + 0.5 - middle) / half) ** ROUNDNESS
            if g < 0.8:
                cover[y * size + x] = 1.0
            elif g < 1.25:
                cover[y * size + x] = sum(inside(x + sx, y + sy) for sx in samples for sy in samples) / 16

    # Below the tile, softly: Ada's own icon plate stands on one too.
    drop, blur = max(1, round(size * 0.012)), max(1, round(size * 0.016))
    shadow = [0.0] * (size * size)
    for y in range(size - drop):
        shadow[(y + drop) * size:(y + drop + 1) * size] = cover[y * size:(y + 1) * size]
    for _ in range(2):
        shadow = _box_blur(shadow, size, blur)

    spans, edges = [], []
    for y in range(size):
        row = cover[y * size:(y + 1) * size]
        full = [x for x in range(size) if row[x] >= 1.0]
        spans.append((full[0], full[-1] + 1) if full else (0, 0))
        for x in range(size):
            m = row[x]
            if m >= 1.0:
                continue
            a = m + shadow[y * size + x] * SHADOW * (1 - m)
            if a > 0.002:
                edges.append((x, y, m, a))
    return Outline(side, offset, spans, edges)


def _box_blur(values: list[float], size: int, radius: int) -> list[float]:
    span = 2 * radius + 1
    across = [0.0] * len(values)
    for y in range(size):
        row = values[y * size:(y + 1) * size]
        total = sum(row[:radius])
        for x in range(size):
            if x + radius < size:
                total += row[x + radius]
            if x - radius - 1 >= 0:
                total -= row[x - radius - 1]
            across[y * size + x] = total / span
    down = [0.0] * len(values)
    for x in range(size):
        total = sum(across[y * size + x] for y in range(radius))
        for y in range(size):
            if y + radius < size:
                total += across[(y + radius) * size + x]
            if y - radius - 1 >= 0:
                total -= across[(y - radius - 1) * size + x]
            down[y * size + x] = total / span
    return down


def _gradient(side: int, top_left, top_right, bottom_left, bottom_right) -> GdkPixbuf.Pixbuf:
    """An opaque square shading between four corner colours, each corner its own colour."""
    corners = bytes([*top_left, *top_right, *bottom_left, *bottom_right])
    small = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(corners), GdkPixbuf.Colorspace.RGB, False, 8, 2, 2, 6)
    return small.scale_simple(side, side, GdkPixbuf.InterpType.BILINEAR).add_alpha(False, 0, 0, 0)


def backdrop(shape: Shape, plain: bool = False) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The tile's colour at its top and bottom, for a picture set on it: a pale wash of the picture's
    own colour (plain: grey). A light picture stands on its colour itself, darkened until it shows."""
    ink = None if plain else shape.ink
    if shape.light > 0.78:
        if not ink:
            return NEUTRAL_DARK
        darker = 0.0
        while darker < 0.7 and _luma(_mix(ink, BLACK, darker)) > shape.light - 0.3:
            darker += 0.05
        return _mix(ink, BLACK, darker), _mix(ink, BLACK, darker + 0.12)
    return (_mix(ink, WHITE, 0.9), _mix(ink, WHITE, 0.78)) if ink else NEUTRAL_LIGHT


def render(path: str, size: int = SIZE, plain: bool = False) -> GdkPixbuf.Pixbuf:
    """An app's icon in the shared shape, `size` pixels square."""
    with open(path, "rb") as handle:
        picture, _native = decode(handle.read(), DRAWN)
    shape = analyze(picture)
    shape_of = outline(size)
    side, offset = shape_of.side, shape_of.offset
    drawn = picture.get_width()
    bx, by, bw, bh = shape.box
    if shape.kind == FILL:
        tile = _gradient(side, *shape.corners)
        # The solid part fills the tile.
        scale_x, scale_y = side / (bw * drawn), side / (bh * drawn)
        left, top = -bx * drawn * scale_x, -by * drawn * scale_y
    else:
        top_colour, bottom_colour = backdrop(shape, plain)
        tile = _gradient(side, top_colour, top_colour, bottom_colour, bottom_colour)
        # Set on the tile at a size that looks even from shape to shape: a sparse triangle
        # grows past a dense square, within bounds.
        presence = math.sqrt(bw * bh * shape.coverage)
        scale = min(0.6 / presence, 0.76 / max(bw, bh)) * side / drawn
        scale_x = scale_y = scale
        left = side / 2 - (bx + bw / 2) * drawn * scale
        top = side / 2 - (by + bh / 2) * drawn * scale
    picture.composite(tile, 0, 0, side, side, left, top, scale_x, scale_y, GdkPixbuf.InterpType.HYPER, 255)

    # The tile, cut to the outline, over its shadow. The tile is opaque: rows inside it are copied whole.
    source, stride = tile.get_pixels(), tile.get_rowstride()
    out = bytearray(size * size * 4)
    for y, (start, end) in enumerate(shape_of.spans):
        if end > start:
            row = (y - offset) * stride
            out[(y * size + start) * 4:(y * size + end) * 4] = source[row + (start - offset) * 4:row + (end - offset) * 4]
    for x, y, m, a in shape_of.edges:
        o = (y * size + x) * 4
        if m > 0:
            t = (y - offset) * stride + (x - offset) * 4
            share = m / a
            out[o] = int(source[t] * share + 0.5)
            out[o + 1] = int(source[t + 1] * share + 0.5)
            out[o + 2] = int(source[t + 2] * share + 0.5)
        out[o + 3] = int(a * 255 + 0.5)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(out)), GdkPixbuf.Colorspace.RGB, True, 8,
                                           size, size, size * 4)


# --- The pack ------------------------------------------------------------------


def source(app: App) -> Optional[str]:
    """The picture an app shows now. Ada draws its own apps from its own folder, by icon name."""
    if ADA_OWN.match(app.id) and app.icon and not app.icon.startswith("/"):
        for ext in (".svg", ".png"):
            path = ADA_ICONS / (app.icon + ext)
            if path.is_file():
                return str(path)
    return app.state.path


def manifest_path() -> Path:
    return store_dir() / "same-shape.json"


def _read_manifest() -> dict:
    try:
        return json.loads(manifest_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_json(target: Path, value) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(value, indent=4, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(temporary, target)


def default_folder() -> Path:
    """Ada's icon packs, where Settings → Look → Icons finds it; elsewhere a folder of IconFetch's own."""
    return data_home() / "ada/icon-packs" / PACK_ID if on_ada() else store_dir() / "same-shape"


@dataclass
class Report:
    folder: Path
    made: list[str] = field(default_factory=list)  # app names
    kept: list[str] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)  # app name, why


def build(folder: Optional[Path] = None, plain: bool = False, apps: Optional[list[App]] = None,
          progress: Optional[Callable[[int, int], None]] = None) -> Report:
    """Write (or bring up to date) the pack: pack.json and icons/<desktop ID>.png for every app.
    Icons whose source hasn't changed since the last build are kept. IconFetch keeps the last pack
    it built up to date (refresh)."""
    with _building:
        return _build(Path(folder or default_folder()), plain, apps, progress)


_building = threading.Lock()  # the window may refresh the pack while it's being made


def _build(folder: Path, plain: bool, apps: Optional[list[App]], progress) -> Report:
    manifest = _read_manifest()
    if apps is None:
        apps = installed_apps(include_ada=True, include_self=True)
    same = manifest.get("folder") == str(folder) and manifest.get("style") == STYLE and manifest.get("plain") == plain
    made_before = manifest.get("icons", {}) if same else {}

    icons = folder / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "pack.json", PACK)
    report, made_now = Report(folder), {}
    for done, app in enumerate(apps, start=1):
        if progress:
            progress(done, len(apps))
        path = source(app)
        if not path:
            report.left_out.append((app.name, "no icon"))
            continue
        try:
            stat = os.stat(path)
        except OSError:
            report.left_out.append((app.name, "icon file missing"))
            continue
        target = icons / f"{app.stem}.png"
        signature = [path, stat.st_mtime_ns, stat.st_size]
        if made_before.get(app.id) == signature and target.is_file():
            made_now[app.id] = signature
            report.kept.append(app.name)
            continue
        try:
            ok, png = render(path, plain=plain).save_to_bufferv("png", [], [])
            if not ok:
                raise ValueError("could not encode PNG")
            # Written here, not by GdkPixbuf: its saver turns down long names (Windows apps' IDs are).
            temporary = icons / f".{target.name}.tmp"
            temporary.write_bytes(bytes(png))
            os.replace(temporary, target)
        except (OSError, ValueError, GLib.Error) as error:
            report.left_out.append((app.name, str(getattr(error, "message", error))))
            continue
        made_now[app.id] = signature
        report.made.append(app.name)

    # Apps uninstalled since: their icons go.
    wanted = {f"{desktop_id.removesuffix('.desktop')}.png" for desktop_id in made_now}
    for old in icons.glob("*.png"):
        if old.name not in wanted:
            old.unlink(missing_ok=True)
    manifest.update(folder=str(folder), style=STYLE, plain=plain, icons=made_now)
    _write_json(manifest_path(), manifest)
    return report


def refresh() -> Optional[Report]:
    """Bring a pack made before up to date with installs, removals and changed icons; None without one."""
    manifest = _read_manifest()
    if "folder" not in manifest or not Path(manifest["folder"]).is_dir():
        return None
    return build(Path(manifest["folder"]), bool(manifest.get("plain")))


# --- Wearing it in Ada ------------------------------------------------------------


def look_file() -> Optional[Path]:
    """The look Ada wears now, when it's one of the person's own (a built-in look can't be changed)."""
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(config_home() / "ada/look.ini")
        active = ini.get("Look", "Active", fallback="")
    except configparser.Error:
        return None
    path = data_home() / "ada/looks" / active / "look.json"
    return path if active and "/" not in active and path.is_file() else None


def _read_look(path: Path) -> dict:
    look = json.loads(path.read_text())
    if not isinstance(look, dict):
        raise ValueError("look.json isn't an object")
    return look


def worn() -> bool:
    path = look_file()
    if path is None:
        return False
    try:
        icons = _read_look(path).get("icons")
    except (OSError, ValueError):
        return False
    return icons == PACK_ID or (isinstance(icons, dict) and icons.get("pack") == PACK_ID)


def wear() -> None:
    """Make Ada's look wear the pack, remembering the icons it wore before (for take_off)."""
    path = look_file()
    if path is None:
        raise ValueError("Ada is wearing one of its built-in looks. Choose “Same shape” in Settings → Look → Icons.")
    if worn():
        return
    look = _read_look(path)
    with _building:
        manifest = _read_manifest()
        manifest["before"] = {"look": str(path), "icons": look.get("icons")}
        _write_json(manifest_path(), manifest)
    look["icons"] = PACK_ID
    _write_json(path, look)


def take_off() -> None:
    """Put back the icons the look wore before the pack (each app's own if nothing is known)."""
    path = look_file()
    if path is None or not worn():
        return
    look = _read_look(path)
    before = _read_manifest().get("before") or {}
    if before.get("look") == str(path) and before.get("icons") is not None:
        look["icons"] = before["icons"]
    else:
        look.pop("icons", None)
    _write_json(path, look)

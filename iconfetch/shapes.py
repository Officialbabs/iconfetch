"""One shape for every icon: an Ada icon pack that brings each app's icon to one shape.

Three shapes to choose from: Ada's own (a rounded square, |x|^5 + |y|^5 = 1,
82% the width of the picture, no shadow), macOS's (Apple's app icon template:
continuous corners, 824 of 1024 pixels, a soft shadow) and a full circle. An
icon already in the shape and size is left as it is (the pack leaves it out).
One in the shape at another size is only resized. A tile of another outline (a
sharp square, a round badge of one colour) is cut to the shape, its corners
filled in its own edge colour. Any other shape (a triangle, a gear, a folder) is
set on a tile in its own main colour. The pictures stay the apps' own.
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
DRAWN = 512  # pixels a source is drawn at before it's fitted to the tile
LOOKED = 128  # pixels a source is looked at to tell its shape
STYLE = 3  # bump when the drawing changes: every icon is made again

KEEP, RESIZE, CUT, SET = "keep", "resize", "cut", "set"
# Ada's own apps take their pictures from the desktop's own folder, by icon name.
ADA_ICONS = Path("/usr/share/ada/qml/icons")
PACK_ID = "iconfetch-same-shape"


def _superellipse(n: float, steps: int = 720) -> list[tuple[float, float]]:
    """|x|^n + |y|^n = 1 round a unit square: 2 is a circle, higher is squarer."""
    points = []
    for i in range(steps):
        c, s = math.cos(2 * math.pi * i / steps), math.sin(2 * math.pi * i / steps)
        points.append((0.5 + 0.5 * math.copysign(abs(c) ** (2 / n), c), 0.5 + 0.5 * math.copysign(abs(s) ** (2 / n), s)))
    return points


def _bezier(p0, p1, p2, p3, steps: int = 24) -> list[tuple[float, float]]:
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3)) for t in (i / steps for i in range(1, steps + 1))]


def _continuous(radius: float = 0.225) -> list[tuple[float, float]]:
    """Apple's rounded square with continuous corners round a unit square (the curve UIKit and AppKit
    draw; the constants as PaintCode measured them). The app icon template's corner: 185.4 of 824."""
    r = radius
    start = (1 - 1.52866483 * r, 0.0)
    corner = [start]  # the top right corner, from the top edge down to the right one
    corner += _bezier(start, (1 - 1.08849323 * r, 0), (1 - 0.86840689 * r, 0.02305800 * r), (1 - 0.63149399 * r, 0.07491100 * r))
    corner += _bezier(corner[-1], (1 - 0.37282392 * r, 0.16157023 * r), (1 - 0.16157023 * r, 0.37282392 * r),
                      (1 - 0.07491100 * r, 0.63149399 * r))
    corner += _bezier(corner[-1], (1 - 0.02305800 * r, 0.86840689 * r), (1, 1.08849323 * r), (1, 1.52866483 * r))
    return (corner + [(x, 1 - y) for x, y in reversed(corner)] + [(1 - x, 1 - y) for x, y in corner]
            + [(1 - x, y) for x, y in reversed(corner)])


@dataclass(frozen=True)
class Form:
    title: str
    trace: Callable[[], list[tuple[float, float]]]  # its outline round a unit square: a convex polygon
    tile: float  # its width, of the picture's
    shadow: float  # darkness of the soft shadow below it (0: none)
    presence: float  # how large a picture set on it looks, of its width (see render)
    reach: float  # the most of its width a picture set on it may span
    about: str

    @functools.cached_property
    def points(self) -> list[tuple[float, float]]:
        return self.trace()


FORMS = {
    "ada": Form("Ada", lambda: _superellipse(5), 0.82, 0.0, 0.6, 0.76, "a rounded square, like Ada's own app icons"),
    "macos": Form("macOS", _continuous, 824 / 1024, 0.3, 0.6, 0.76,
                  "Apple's rounded square with continuous corners and a soft shadow"),
    # As large as the squares look: about the same area.
    "circle": Form("Circle", lambda: _superellipse(2), 0.9, 0.0, 0.55, 0.66, "a full circle"),
}
DEFAULT_FORM = "ada"


def pack_about(form: str) -> dict:
    return {
        "name": "Same shape",
        "about": f"Every app's own icon in one shape: {FORMS[form].about}. Made by IconFetch, which keeps it up to date.",
        "author": "IconFetch",
    }


@dataclass
class Shape:
    kind: str  # KEEP, RESIZE, CUT or SET: what the icon needs to take the shared shape
    box: tuple[float, float, float, float]  # the solid part: x, y, width, height, as fractions of the picture
    coverage: float  # how much of that box is solid
    corners: tuple[tuple[int, int, int], ...]  # edge colour near each corner: top left, top right, bottom left, bottom right
    colour: Optional[tuple[float, float, float]]  # its main colour as OKLCh: lightness, chroma, hue; None when grey
    light: float  # how light the picture is overall (OKLab lightness, 0 to 1)


# --- Colour --------------------------------------------------------------------
# OKLab (Björn Ottosson's): even steps of lightness and hue, so a colour can be
# made lighter or darker and still look like itself.


def _linear(c: int) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def oklab(rgb) -> tuple[float, float, float]:
    r, g, b = (_linear(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _srgb(lab) -> Optional[tuple[int, int, int]]:
    """An OKLab colour as sRGB, or None when the screen can't show it."""
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
              -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
              -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    if any(c < -0.0005 or c > 1.0005 for c in linear):
        return None
    encoded = (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in (max(0.0, min(1.0, c)) for c in linear))
    return tuple(round(c * 255) for c in encoded)


def lch(lightness: float, chroma: float, hue: float) -> tuple[int, int, int]:
    """An OKLCh colour as sRGB, its chroma lowered as far as the screen needs."""
    angle = math.radians(hue)

    def shown(c: float) -> Optional[tuple[int, int, int]]:
        return _srgb((lightness, c * math.cos(angle), c * math.sin(angle)))

    if shown(chroma) is not None:
        return shown(chroma)
    low, high = 0.0, chroma
    for _ in range(16):
        middle = (low + high) / 2
        low, high = (middle, high) if shown(middle) is not None else (low, middle)
    return shown(low)


def _main_colour(pixels: list[tuple[float, float, float, float]]) -> Optional[tuple[float, float, float]]:
    """The colour a picture reads as: the hue its vivid pixels gather around most, as OKLCh. None when
    hardly anything in it is coloured. `pixels`: OKLab lightness, a, b and opacity of each solid pixel."""
    bins = [0.0] * 36
    coloured = total = 0.0
    hues = []
    for L, a, b, alpha in pixels:
        total += alpha
        chroma = math.hypot(a, b)
        if chroma < 0.03:
            continue
        hue = math.degrees(math.atan2(b, a)) % 360
        coloured += alpha
        bins[int(hue // 10) % 36] += alpha * chroma
        hues.append((hue, L, chroma, alpha))
    if not total or coloured < 0.12 * total:
        return None
    smooth = [bins[i - 1] + 2 * bins[i] + bins[(i + 1) % 36] for i in range(36)]
    peak = (smooth.index(max(smooth)) + 0.5) * 10
    near = [(h, L, c, w) for h, L, c, w in hues if abs((h - peak + 180) % 360 - 180) <= 30]
    weight = sum(w for *_, w in near)
    x = sum(w * c * math.cos(math.radians(h)) for h, _, c, w in near) / weight
    y = sum(w * c * math.sin(math.radians(h)) for h, _, c, w in near) / weight
    lightness = sum(w * L for _, L, _, w in near) / weight
    chroma = sum(w * c for _, _, c, w in near) / weight
    return lightness, chroma, math.degrees(math.atan2(y, x)) % 360


def tile_colours(shape: Shape, plain: bool = False) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The tile's colour at its top and bottom, for a picture set on it: a light tint of the picture's
    own main colour. A mostly white picture would vanish on that: it gets its colour in full instead,
    deep enough to stand out (grey for a grey picture, or with `plain`)."""
    whitish = shape.light > 0.85
    if plain or shape.colour is None:
        return (lch(0.45, 0, 0), lch(0.38, 0, 0)) if whitish else (lch(0.975, 0, 0), lch(0.915, 0, 0))
    _lightness, chroma, hue = shape.colour
    if whitish:
        deep = min(0.7, shape.light - 0.25)
        return lch(deep, chroma, hue), lch(deep - 0.06, chroma, hue)
    return lch(0.955, min(chroma * 0.4, 0.07), hue), lch(0.895, min(chroma * 0.55, 0.095), hue)


# --- Looking -------------------------------------------------------------------


def _spans(points, left: float, top: float, width: float, height: float, y: float) -> Optional[tuple[float, float]]:
    """Where a horizontal line at `y` crosses a convex outline fitted to a box: its left and right x."""
    crossings = []
    for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
        ay, by = top + ay * height, top + by * height
        if (ay <= y < by) or (by <= y < ay):
            ax, bx = left + ax * width, left + bx * width
            crossings.append(ax + (y - ay) * (bx - ax) / (by - ay))
    return (min(crossings), max(crossings)) if len(crossings) >= 2 else None


def _outline_match(alpha, form: Form, x0: int, y0: int, width: int, height: int) -> float:
    """How closely the solid part fills the shape drawn in its box (intersection over union)."""
    both = either = 0
    for y in range(y0, y0 + height):
        span = _spans(form.points, x0, y0, width, height, y + 0.5)
        row = alpha[y]
        for x in range(x0, x0 + width):
            inside = span is not None and span[0] <= x + 0.5 <= span[1]
            solid = row[x] >= 128
            both += inside and solid
            either += inside or solid
    return both / either if either else 0.0


def analyze(pixbuf: GdkPixbuf.Pixbuf, form: str = DEFAULT_FORM) -> Shape:
    """Tell a square picture's shape: what it needs to take the `form` one, where its solid part is,
    its colours."""
    shape_of = FORMS[form]
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
    pixels = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            a = alpha[y][x]
            if a < 128:
                continue
            i = y * stride + x * 4
            rgb = (data[i], data[i + 1], data[i + 2])
            pixels.append((*oklab(rgb), a / 255))
            if a >= 240 and (outside(x - step, y) or outside(x + step, y) or outside(x, y - step) or outside(x, y + step)):
                quarters[(y >= (y0 + y1) / 2) * 2 + (x >= (x0 + x1) / 2)].append(rgb)

    everything = [rgb for quarter in quarters for rgb in quarter]
    corners, steady = [], True
    for quarter in quarters:
        quarter = quarter or everything or [(255, 255, 255)]
        mean = tuple(round(sum(c[k] for c in quarter) / len(quarter)) for k in range(3))
        near = sum(1 for c in quarter if max(abs(c[k] - mean[k]) for k in range(3)) <= 36)
        steady = steady and near >= 0.85 * len(quarter)
        corners.append(mean)
    # One colour all round, or one shading gently into another: not a logo of many colours.
    steady = steady and all(max(abs(a[k] - b[k]) for k in range(3)) <= 60 for a in corners for b in corners)

    squarish = 0.9 <= width / height <= 1 / 0.9
    if squarish and _outline_match(alpha, shape_of, x0, y0, width, height) >= 0.975:
        # Already the shape: at its size, in the middle, it's left as it is.
        centred = abs((x0 + x1) / 2 / side - 0.5) <= 0.02 and abs((y0 + y1) / 2 / side - 0.5) <= 0.02
        kind = KEEP if centred and abs(max(width, height) / side - shape_of.tile) <= 0.03 else RESIZE
    elif squarish and (coverage >= 0.86 or (coverage >= 0.74 and steady)):
        # A tile of another outline (a sharp square about 1, a rounded one about 0.95) or a badge of
        # one colour (about 0.79).
        kind = CUT
    else:
        kind = SET
    box = (x0 / side, y0 / side, width / side, height / side)
    weight = sum(p[3] for p in pixels)
    light = sum(p[0] * p[3] for p in pixels) / weight
    return Shape(kind, box, coverage, tuple(corners), _main_colour(pixels), light)


# --- Drawing -------------------------------------------------------------------


@dataclass
class Outline:
    side: int  # the tile's width in the icon
    offset: int  # its distance from the icon's edges
    spans: list[tuple[int, int]]  # each row's run of pixels wholly inside the tile (empty: none)
    edges: list[tuple[int, int, float, float]]  # every other pixel that shows: x, y, tile cover, opacity with shadow


@functools.lru_cache(maxsize=8)
def outline(form: str = DEFAULT_FORM, size: int = SIZE) -> Outline:
    """Where the tile and any shadow fall in a `size` icon. Its edge is smoothed exactly across and
    in eight steps down each pixel."""
    shape_of = FORMS[form]
    side = round(size * shape_of.tile)
    offset = (size - side) // 2
    cover = [0.0] * (size * size)
    steps = 8
    for y in range(offset - 1, offset + side + 1):
        for k in range(steps):
            span = _spans(shape_of.points, offset, offset, side, side, y + (k + 0.5) / steps)
            if span is None:
                continue
            left, right = span
            for x in range(max(0, int(left)), min(size, int(right) + 1)):
                share = min(right, x + 1) - max(left, x)
                if share > 0:
                    cover[y * size + x] += share / steps

    shadow = [0.0] * (size * size)
    if shape_of.shadow:
        # Below the tile, softly, as in Apple's template: 10 pixels down and blurred by 10, of 1024.
        drop, blur = max(1, round(size * 10 / 1024)), max(1, round(size * 5 / 1024))
        for y in range(size - drop):
            shadow[(y + drop) * size:(y + drop + 1) * size] = cover[y * size:(y + 1) * size]
        for _ in range(3):
            shadow = _box_blur(shadow, size, blur)

    spans, edges = [], []
    for y in range(size):
        row = cover[y * size:(y + 1) * size]
        full = [x for x in range(size) if row[x] >= 0.999]
        spans.append((full[0], full[-1] + 1) if full else (0, 0))
        for x in range(size):
            m = min(1.0, row[x])
            if m >= 0.999:
                continue
            a = m + shadow[y * size + x] * shape_of.shadow * (1 - m)
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


def render(path: str, size: int = SIZE, plain: bool = False, form: str = DEFAULT_FORM) -> Optional[GdkPixbuf.Pixbuf]:
    """An app's icon in the `form` shape, `size` pixels square; None when it already has it."""
    with open(path, "rb") as handle:
        picture, _native = decode(handle.read(), DRAWN)
    shape = analyze(picture, form)
    if shape.kind == KEEP:
        return None
    shape_of = outline(form, size)
    side, offset = shape_of.side, shape_of.offset
    drawn = picture.get_width()
    bx, by, bw, bh = shape.box
    if shape.kind == RESIZE:
        # Its own outline, at the tile's size and place. Copied in, not blended: GdkPixbuf blends
        # onto a clear picture badly.
        scale = side / (max(bw, bh) * drawn)
        left = size / 2 - (bx + bw / 2) * drawn * scale
        top = size / 2 - (by + bh / 2) * drawn * scale
        x0, y0 = max(0, math.ceil(left)), max(0, math.ceil(top))
        x1, y1 = min(size, math.floor(left + drawn * scale)), min(size, math.floor(top + drawn * scale))
        canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
        canvas.fill(0)
        picture.scale(canvas, x0, y0, x1 - x0, y1 - y0, left, top, scale, scale, GdkPixbuf.InterpType.HYPER)
        return canvas
    if shape.kind == CUT:
        tile = _gradient(side, *shape.corners)
        # The solid part fills the tile.
        scale_x, scale_y = side / (bw * drawn), side / (bh * drawn)
        left, top = -bx * drawn * scale_x, -by * drawn * scale_y
    else:
        top_colour, bottom_colour = tile_colours(shape, plain)
        tile = _gradient(side, top_colour, top_colour, bottom_colour, bottom_colour)
        # Set on the tile at a size that looks even from shape to shape: a sparse triangle
        # grows past a dense square, within bounds.
        presence = math.sqrt(bw * bh * shape.coverage)
        scale = min(FORMS[form].presence / presence, FORMS[form].reach / max(bw, bh)) * side / drawn
        scale_x = scale_y = scale
        left = side / 2 - (bx + bw / 2) * drawn * scale
        top = side / 2 - (by + bh / 2) * drawn * scale
    picture.composite(tile, 0, 0, side, side, left, top, scale_x, scale_y, GdkPixbuf.InterpType.HYPER, 255)

    # The tile, cut to the outline, over any shadow. It's opaque: rows inside it are copied whole.
    source, stride = tile.get_pixels(), tile.get_rowstride()
    out = bytearray(size * size * 4)
    for y, (start, end) in enumerate(shape_of.spans):
        if end > start:
            row = (y - offset) * stride
            out[(y * size + start) * 4:(y * size + end) * 4] = source[row + (start - offset) * 4:row + (end - offset) * 4]
    for x, y, m, a in shape_of.edges:
        o = (y * size + x) * 4
        if m > 0:
            # The tile's colour, darkened only by the shadow showing through its smoothed edge.
            t, share = (y - offset) * stride + (x - offset) * 4, m / a
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
    made: list[str] = field(default_factory=list)  # app names: icons drawn now
    unchanged: list[str] = field(default_factory=list)  # drawn before, from the same source
    as_is: list[str] = field(default_factory=list)  # already in the shape: left out, so Ada shows their own
    left_out: list[tuple[str, str]] = field(default_factory=list)  # app name, why

    @property
    def shaped(self) -> int:
        return len(self.made) + len(self.unchanged)


def build(folder: Optional[Path] = None, plain: bool = False, apps: Optional[list[App]] = None,
          progress: Optional[Callable[[int, int], None]] = None, form: Optional[str] = None) -> Report:
    """Write (or bring up to date) the pack: pack.json and icons/<desktop ID>.png for every app whose
    icon isn't in the shape yet. Icons whose source hasn't changed since the last build aren't looked
    at again. `form`: ada, macos or circle (the last one used when not given). IconFetch keeps the last
    pack it built up to date (refresh)."""
    form = form or last_form()
    if form not in FORMS:
        raise ValueError(f"no shape called {form}: choose {', '.join(FORMS)}")
    with _building:
        return _build(Path(folder or default_folder()), plain, apps, progress, form)


_building = threading.Lock()  # the window may refresh the pack while it's being made


def last_form() -> str:
    """The shape the pack was last made in."""
    form = _read_manifest().get("form")
    return form if form in FORMS else DEFAULT_FORM


def _build(folder: Path, plain: bool, apps: Optional[list[App]], progress, form: str) -> Report:
    manifest = _read_manifest()
    if apps is None:
        apps = installed_apps(include_ada=True, include_self=True)
    same = (manifest.get("folder") == str(folder) and manifest.get("style") == STYLE and manifest.get("plain") == plain
            and manifest.get("form") == form)
    made_before = manifest.get("icons", {}) if same else {}
    as_is_before = set(manifest.get("as_is", [])) if same else set()

    icons = folder / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    _write_json(folder / "pack.json", pack_about(form))
    report, made_now, as_is = Report(folder), {}, []
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
        if made_before.get(app.id) == signature and (app.id in as_is_before or target.is_file()):
            made_now[app.id] = signature
            if app.id in as_is_before:
                as_is.append(app.id)
                report.as_is.append(app.name)
            else:
                report.unchanged.append(app.name)
            continue
        try:
            pixbuf = render(path, plain=plain, form=form)
            if pixbuf is None:
                made_now[app.id] = signature
                as_is.append(app.id)
                report.as_is.append(app.name)
                continue
            ok, png = pixbuf.save_to_bufferv("png", [], [])
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

    # Apps uninstalled since, and icons in the shape now: their pictures go.
    wanted = {f"{desktop_id.removesuffix('.desktop')}.png" for desktop_id in made_now if desktop_id not in as_is}
    for old in icons.glob("*.png"):
        if old.name not in wanted:
            old.unlink(missing_ok=True)
    manifest.update(folder=str(folder), style=STYLE, plain=plain, form=form, icons=made_now, as_is=sorted(as_is))
    _write_json(manifest_path(), manifest)
    return report


def refresh() -> Optional[Report]:
    """Bring a pack made before up to date with installs, removals and changed icons; None without one."""
    manifest = _read_manifest()
    if "folder" not in manifest or not Path(manifest["folder"]).is_dir():
        return None
    return build(Path(manifest["folder"]), bool(manifest.get("plain")), form=last_form())


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

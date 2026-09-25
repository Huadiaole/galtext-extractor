#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate every raster asset for the GalText Extractor Tkinter desktop app.

Usage
-----
    python scripts/make_assets.py            # (re)draw all assets
    python scripts/make_assets.py --check    # verify only, never redraw

Design notes
------------
* Deterministic: no randomness, no timestamps, no locale-dependent formatting.
  PNG/ICO bytes are stable across runs (verified by the caller).
* Antialiasing: every shape is drawn on a 4x supersampled canvas and reduced
  with ``Image.LANCZOS``.  Nothing is ever drawn directly at final size --
  the only exception is the final downscale target itself.
* Dependencies: Pillow + the Python standard library.  Nothing else.
"""

from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------
ACCENT = (0x4F, 0x6B, 0xED)          # #4F6BED indigo
ACCENT_HOVER = (0x3D, 0x57, 0xD6)    # #3D57D6
ACCENT2 = (0x8B, 0x5C, 0xF6)         # #8B5CF6 violet
ACCENT3 = (0x22, 0xD3, 0xEE)         # #22D3EE cyan
INK = (0x17, 0x1A, 0x22)             # #171A22
INK_MUTED = (0x6B, 0x72, 0x80)       # #6B7280
DARK_BG = (0x14, 0x16, 0x1C)         # #14161C
DARK_SURFACE = (0x1C, 0x1F, 0x27)    # #1C1F27
DARK_BORDER = (0x2B, 0x2F, 0x3A)     # #2B2F3A
DARK_TEXT = (0xE9, 0xEB, 0xF2)       # #E9EBF2
DARK_MUTED = (0x98, 0xA0, 0xB0)      # #98A0B0
LIGHT_BG = (0xF5, 0xF6, 0xFA)        # #F5F6FA
LIGHT_SURFACE = (0xFF, 0xFF, 0xFF)   # #FFFFFF
LIGHT_BORDER = (0xE4, 0xE7, 0xF0)    # #E4E7F0
BANNER_GLOW = (0x1E, 0x22, 0x33)     # #1E2233

ICON_STROKE = {
    "light": (0x3A, 0x41, 0x52),     # #3A4152
    "dark": (0xC6, 0xCC, 0xDA),      # #C6CCDA
    "accent": (0xFF, 0xFF, 0xFF),    # #FFFFFF
}

APP_SIZES = [16, 20, 24, 32, 48, 64, 128, 256]
# Pillow's ICO writer wants explicit (width, height) tuples.
ICO_SIZES = [(s, s) for s in (16, 20, 24, 32, 48, 64, 128, 256)]
SS = 4                                # supersample factor
TOOLBAR_NAMES = [
    "folder", "scan", "stop", "export", "copy", "search",
    "sun", "moon", "pdf", "font", "info", "grid",
]

# CJK string constants, escaped so this source file stays pure ASCII and is
# immune to console/codepage mangling.
LABEL_TOOLBAR_LIGHT = "\u5de5\u5177\u680f\uff08\u6d45\u8272\uff09"   # 工具栏（浅色）
LABEL_TOOLBAR_DARK = "\u5de5\u5177\u680f\uff08\u6df1\u8272\uff09"     # 工具栏（深色）
BANNER_SUBTITLE = (
    "GAL \u6587\u672c\u63d0\u53d6\u5668 \u00b7 \u4ece galgame \u76ee\u5f55"
    "\u63d0\u53d6\u5bf9\u8bdd\uff0c\u6392\u7248\u6210\u5267\u672c PDF"
)                                                                    # GAL 文本提取器 · 从 galgame 目录提取对话，排版成剧本 PDF
BANNER_CREDIT = (
    "by \u5168\u90e8\u5316\u6389\u4e86  \u00b7  MIT License  \u00b7  "
    "\u96f6\u8fd0\u884c\u65f6\u4f9d\u8d56"
)                                                                    # by 全部化掉了 · MIT License · 零运行时依赖
BANNER_CHIPS = [
    "XP3",
    "NSA/SAR",
    "BGI arc",
]
CHIP_L = "\u300c"   # 「
CHIP_R = "\u300d"   # 」

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "galtext", "assets")
ICONS = os.path.join(ASSETS, "icons")
DOCS = os.path.join(ROOT, "docs")


def p(*parts: str) -> str:
    return os.path.join(*parts)


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


# --------------------------------------------------------------------------
# low-level drawing helpers (all coordinates are *final-size* floats)
# --------------------------------------------------------------------------
def new_rgba(size):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def down(img, size):
    """LANCZOS downscale of a 4x supersampled canvas to the final size."""
    return img.resize(size, Image.LANCZOS)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def diagonal_gradient(size, c0, c1):
    """Diagonal (top-left -> bottom-right) linear gradient.

    Implemented by hand: every scan line is filled with the colour
    interpolated at t = (y + x_mid) / (w + h - 2), drawn as a 1px line.
    """
    w, h = size
    g = Image.new("RGB", size)
    d = ImageDraw.Draw(g)
    span = max(1, (w - 1) + (h - 1))
    for y in range(h):
        for x in range(0, w, 64):          # 1px-tall runs of interpolated colour
            t = (y + x) / span
            d.line([(x, y), (min(w - 1, x + 63), y)], fill=lerp(c0, c1, t))
    return g.convert("RGBA")


def rounded_mask(size, box, radius):
    """Supersampled alpha mask of a rounded rectangle."""
    ss = size[0] * SS, size[1] * SS
    m = Image.new("L", ss, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [box[0] * SS, box[1] * SS, box[2] * SS, box[3] * SS],
        radius=radius * SS, fill=255,
    )
    return m.resize(size, Image.LANCZOS)


def chevron(d, cx, cy, w, h, width, up=True):
    """V-shaped arrow head centred on (cx, cy)."""
    s = -1 if up else 1
    d.line([(cx - w / 2, cy + s * h / 2), (cx, cy - s * h / 2),
            (cx + w / 2, cy + s * h / 2)], fill=255,
           width=int(width), joint="curve")


# --------------------------------------------------------------------------
# 1) / 2) app icon
# --------------------------------------------------------------------------
def app_icon(size: int) -> Image.Image:
    """The GalText Extractor app tile: gradient tile + white speech bubble."""
    s = float(size)
    tile = diagonal_gradient((size, size), ACCENT, ACCENT2)

    # rounded-square alpha mask for the whole tile
    mask = rounded_mask((size, size), (0, 0, s - 1, s - 1), 0.22 * s)
    tile.putalpha(mask)

    # soft glassy highlight over the top ~45% of the tile
    hl = Image.new("L", (size, size), 0)
    ImageDraw.Draw(hl).rectangle([0, 0, size, int(0.45 * s)], fill=28)
    hl = ImageChops.multiply(hl, mask)
    tile = Image.alpha_composite(tile, Image.merge("RGBA", (
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255),
        Image.new("L", (size, size), 255),
        hl,
    )))

    # --- speech bubble (drawn into its own layer so the tail seam is hidden)
    # tail first: isoceles triangle pointing down-left, overlapping the body
    tail = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(tail).polygon(
        [(0.30 * s * SS, 0.62 * s * SS),
         (0.42 * s * SS, 0.62 * s * SS),
         (0.29 * s * SS, 0.79 * s * SS)],
        fill=255,
    )
    tail = tail.resize((size, size), Image.LANCZOS)

    body = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(body).rounded_rectangle(
        [0.19 * s * SS, 0.23 * s * SS, 0.81 * s * SS, 0.66 * s * SS],
        radius=0.10 * s * SS, fill=255,
    )
    body = body.resize((size, size), Image.LANCZOS)

    white = Image.new("RGBA", (size, size), (255, 255, 255, 242))
    bubble = Image.composite(white, new_rgba((size, size)),
                             ImageChops.lighter(tail, body))
    tile = Image.alpha_composite(tile, bubble)

    # --- text bars; tiny sizes get 2 fatter bars so they survive 16px
    if size <= 20:
        bars, bar_h = [(0.325, 0.34), (0.415, 0.25)], 0.075
    else:
        bars, bar_h = [(0.325, 0.34), (0.415, 0.25), (0.505, 0.17)], 0.055
    layer = new_rgba((size * SS, size * SS))
    ld = ImageDraw.Draw(layer)
    for y, w in bars:
        ld.rounded_rectangle(
            [0.28 * s * SS, y * s * SS,
             (0.28 + w) * s * SS, (y + bar_h) * s * SS],
            radius=bar_h * s * SS / 2.0, fill=ACCENT + (255,),
        )
    return Image.alpha_composite(tile, down(layer, (size, size)))


# --------------------------------------------------------------------------
# 3) toolbar line-art icons (20x20 logical space, 1.7px stroke)
# --------------------------------------------------------------------------
STROKE_W = 1.7


def _line(d, pts, w=STROKE_W):
    d.line([(x * SS, y * SS) for x, y in pts], fill=255,
           width=int(round(w * SS)), joint="curve")


def _ring(d, box, w=STROKE_W):
    d.ellipse([box[0] * SS, box[1] * SS, box[2] * SS, box[3] * SS],
              outline=255, width=int(round(w * SS)))


def _loop(d, pts, w=STROKE_W):
    """Closed *stroked* path: a line art outline, transparent inside.

    Document and folder bodies are drawn with this rather than a filled
    polygon so they keep the same hollow weight as the rest of the set.
    """
    ring_pts = list(pts) + [pts[0]]
    d.line([(x * SS, y * SS) for x, y in ring_pts], fill=255,
           width=int(round(w * SS)), joint="curve")


def _rrect(d, box, radius, w=STROKE_W):
    d.rounded_rectangle([box[0] * SS, box[1] * SS, box[2] * SS, box[3] * SS],
                        radius=radius * SS, outline=255,
                        width=int(round(w * SS)))


def _dot(d, cx, cy, r):
    d.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS],
              fill=255)


def shape_folder(d):
    """打开的文件夹 -- folder outline with a tab on the top-left.

    The main body is the part of a circle (centre 7.62,12.48 r 10.08) that stays
    inside the bounding box, which yields straight right/bottom edges joined to
    the top edge by a generous rounded right shoulder.
    """
    _ring(d, (-2.46, 2.40, 17.70, 22.56))
    _line(d, [(2.2, 16.2), (2.2, 4.9)])
    _line(d, [(2.2, 4.9), (7.6, 4.9)])
    _line(d, [(7.6, 4.9), (9.1, 7.1)])
    _line(d, [(9.1, 7.1), (17.7, 7.1), (17.7, 16.2), (2.2, 16.2)])


def shape_scan(d):
    """扫描 -- magnifier whose lens contains 3 short horizontal text lines."""
    _ring(d, (3.5, 3.5, 13.5, 13.5))   # centre (8.5, 8.5) r 5
    _line(d, [(12.6, 12.6), (17.2, 17.2)])
    for x1, x2, y in ((8.0, 12.6, 6.8), (8.0, 11.3, 8.5), (8.0, 10.1, 10.2)):
        _line(d, [(x1, y), (x2, y)], 1.25)


def shape_stop(d):
    """停止 -- filled rounded square inside a circle outline."""
    _ring(d, (2.6, 2.6, 17.4, 17.4))
    d.rounded_rectangle([6.9 * SS, 6.9 * SS, 13.1 * SS, 13.1 * SS],
                        radius=1.4 * SS, fill=255)


def shape_export(d):
    """导出 -- tray/box outline with an arrow pointing up out of it."""
    _line(d, [(5.3, 12.0), (3.4, 12.0), (3.4, 16.7), (16.6, 16.7), (16.6, 12.0), (14.7, 12.0)])
    _line(d, [(10.0, 12.5), (10.0, 4.3)])
    chevron(d, 10.0, 3.6, 4.6, 2.8, STROKE_W, up=True)


def shape_copy(d):
    """复制 -- two overlapping rounded rectangles."""
    _rrect(d, (3.0, 3.0, 12.4, 12.4), 2.0)
    _rrect(d, (7.6, 7.6, 17.0, 17.0), 2.0)


def shape_search(d):
    """搜索 -- plain magnifier (circle + diagonal handle)."""
    _ring(d, (3.5, 3.5, 13.5, 13.5))
    _line(d, [(12.6, 12.6), (17.2, 17.2)])


def shape_sun(d):
    """浅色模式 -- circle with 8 short rays."""
    _ring(d, (6.2, 6.2, 13.8, 13.8))
    for i in range(8):
        a = math.radians(i * 45.0)
        ca, sa = math.cos(a), math.sin(a)
        _line(d, [(10 + 4.7 * ca, 10 + 4.7 * sa), (10 + 6.6 * ca, 10 + 6.6 * sa)], 1.45)


def shape_moon(d, canvas):
    """深色模式 -- crescent: filled circle with an offset circle knocked out.

    Both circles are differenced while still on the 4x canvas so the crescent
    keeps clean antialiased edges.  ``ImageDraw.bitmap`` cannot be used here:
    it *thresholds* its mask, which turned the crescent into a solid blob.
    """
    moon = Image.new("L", (20 * SS, 20 * SS), 0)
    md = ImageDraw.Draw(moon)
    md.ellipse([5.0 * SS, 3.2 * SS, 16.6 * SS, 16.8 * SS], fill=255)
    md.ellipse([3.4 * SS, 1.2 * SS, 15.0 * SS, 15.4 * SS], fill=0)
    canvas.paste(255, (0, 0), moon)


def shape_pdf(d):
    """剧本 PDF -- document with a folded top-right corner and 2 text lines.

    Pure line art: the body is a *stroked* closed path (never filled) so the
    interior stays transparent and the weight matches copy/export/font.  The
    fold is implied by the notch in the outline plus one crease line; adding a
    stroked flap as well made this glyph noticeably heavier than its
    neighbours.  Everything (including the text lines) uses the standard
    stroke weight so the set stays visually consistent.
    """
    _loop(d, [(3.9, 2.6), (12.3, 2.6), (16.1, 6.5), (16.1, 17.4), (3.9, 17.4)])
    _line(d, [(12.3, 2.6), (12.3, 6.5), (16.1, 6.5)])          # folded corner
    _line(d, [(7.0, 10.9), (13.0, 10.9)])                      # "text" line 1
    _line(d, [(7.0, 14.1), (13.0, 14.1)])                      # "text" line 2


def shape_font(d):
    """字体 -- capital A from two strokes + crossbar, plus an underline bar."""
    _line(d, [(6.6, 4.0), (4.4, 4.0), (9.8, 14.6), (15.2, 4.0), (13.0, 4.0)])
    _line(d, [(7.4, 10.4), (12.3, 10.4)])
    _line(d, [(4.4, 17.2), (15.6, 17.2)])


def shape_info(d):
    """关于 -- circle outline with a lowercase i (dot + stem)."""
    _ring(d, (2.6, 2.6, 17.4, 17.4))
    _dot(d, 10.0, 6.7, 1.05)
    _line(d, [(10.0, 9.4), (10.0, 14.2)])


def shape_grid(d):
    """引擎列表 -- 2x2 grid of small rounded squares."""
    for x, y in ((2.5, 2.5), (11.1, 2.5), (2.5, 11.1), (11.1, 11.1)):
        _rrect(d, (x, y, x + 6.4, y + 6.4), 1.7)


SHAPES = {
    "folder": shape_folder,
    "scan": shape_scan,
    "stop": shape_stop,
    "export": shape_export,
    "copy": shape_copy,
    "search": shape_search,
    "sun": shape_sun,
    "moon": shape_moon,
    "pdf": shape_pdf,
    "font": shape_font,
    "info": shape_info,
    "grid": shape_grid,
}

# --------------------------------------------------------------------------
# checkbox indicators (used by the built-in ttk theme)
# --------------------------------------------------------------------------
CHECK_SIZE = 16
# 中灰：在浅色 (#F5F6FA) 和深色 (#14161C) 背景上都够清晰，
# 所以未勾选框只用一套图片就能同时服务两种主题。
CHECK_BORDER = (0x8A, 0x93, 0xA6)


def checkbox_icon(checked: bool, size: int = CHECK_SIZE) -> Image.Image:
    """Self-drawn checkbutton indicator.

    Some Tk builds (8.6.15 on this machine) render the *checked* state of the
    clam checkbutton as a thick cross, which reads as an error mark.  The
    built-in theme therefore swaps in these images: checked = accent rounded
    square with a white tick, unchecked = transparent square with a grey
    outline.
    """
    big = size * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    inset = big * 0.07
    box = (inset, inset, big - inset, big - inset)
    radius = big * 0.27
    stroke = max(2, round(big * 0.10))

    if checked:
        d.rounded_rectangle(box, radius=radius, fill=ACCENT + (255,))
        pts = [(big * 0.27, big * 0.53), (big * 0.43, big * 0.69), (big * 0.74, big * 0.34)]
        d.line(pts, fill=(255, 255, 255, 255), width=stroke, joint="curve")
        # Pillow 的 line 没有圆头端点，在折点上补圆点
        half = stroke / 2.0
        for cx, cy in pts:
            d.ellipse((cx - half, cy - half, cx + half, cy + half), fill=(255, 255, 255, 255))
    else:
        d.rounded_rectangle(
            box, radius=radius, fill=(0, 0, 0, 0), outline=CHECK_BORDER + (255,), width=stroke
        )

    return canvas.resize((size, size), Image.LANCZOS)


def toolbar_icon(name: str, colour, size: int = 20) -> Image.Image:
    """Draw one line-art icon at 4x on a 20x20 logical grid, then downsample."""
    canvas = Image.new("L", (20 * SS, 20 * SS), 0)
    d = ImageDraw.Draw(canvas)
    # every shape strokes through ``d``; only the moon, which needs a mask
    # subtraction, also receives the canvas itself
    if name == "moon":
        SHAPES[name](d, canvas)
    else:
        SHAPES[name](d)
    mask = canvas.resize((size, size), Image.LANCZOS)
    # Start from a fully opaque image in the stroke colour and keep the mask as
    # the alpha channel.  Using a (0,0,0,0) base instead would leave the
    # antialiased edge pixels dark, giving the glyphs a dirty halo.
    out = Image.new("RGBA", (size, size), colour + (255,))
    out.putalpha(mask)
    return out


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------
FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\Dengb.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]
FONT_CANDIDATES_REGULAR = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

_FONT_CACHE = {}
_FONT_STATE = {"degraded": False, "bold_path": None, "regular_path": None}


def load_font(size: int, bold: bool = False):
    """First loadable candidate from the OS font list, else Pillow's bitmap font.

    ``index=0`` is required for ``.ttc`` TrueType *collections*: a single file
    can hold several faces (msyh.ttc ships Regular/Bold/Light, Deng.ttf-style
    families do the same).  ``ImageFont.truetype`` needs an explicit face index
    and Pillow's default (the first face, index 0) is the one we want, but we
    pass it explicitly so the behaviour is documented and platform-stable
    rather than relying on the default.
    """
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for cand in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR):
        if os.path.isfile(cand):
            try:
                f = ImageFont.truetype(cand, size, index=0)
            except Exception:
                continue
            _FONT_CACHE[key] = f
            _FONT_STATE["bold_path" if bold else "regular_path"] = cand
            return f
    # graceful degradation: never crash on a font-less machine
    _FONT_STATE["degraded"] = True
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:                      # very old Pillow
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def baseline_y(font, baseline: float) -> float:
    """Top-left y that puts the text baseline at ``baseline``."""
    return baseline - font.getmetrics()[0]


def soft_glow(size, centre, radii, peak, falloff, bands=64):
    """Soft radial accent glow as an L-mode alpha mask.

    Built from concentric translucent ellipses whose alpha decays
    exponentially from ``peak`` at the centre to (almost) 0 at the edge of the
    outer ellipse, then bilinear-smoothed to the final size.  The ellipse is
    sized to fade to ~0 before it leaves the canvas, so there is no clipped
    hard seam at the image border (an earlier version drew a small glow and
    blew it up 8x, which left visible bands and rectangular edges).
    """
    w, h = size
    cx, cy = centre
    rx, ry = radii
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    mid, grow = 0.36, 1.0                     # inner plateau, then falloff
    prev = None
    for i in range(bands):
        t = 1.0 - i / bands
        d = max(0.0, (t - mid) / (1.0 - mid)) if t > mid else 0.0
        a = int(round(peak * math.exp(-falloff * d * d)))
        if a == prev:
            continue
        prev = a
        md.ellipse([cx - t * rx, cy - t * ry, cx + t * rx, cy + t * ry], fill=a)
    return mask


# --------------------------------------------------------------------------
# 4) README banner
# --------------------------------------------------------------------------
CHIP_PAD_X = 18          # >= 14: horizontal padding inside each pill
CHIP_PAD_Y = 7           # vertical padding, so the text never touches the edge
BANNER_W, BANNER_H = 1280, 400
BANNER_MARGIN = 40       # no decoration may come closer than this to the edge


def measure_chip(draw, label, font):
    """Pill size for a chip: text extents via getbbox + padding on all sides."""
    x0, y0, x1, y1 = font.getbbox(label)
    return (x1 - x0) + 2 * CHIP_PAD_X, (y1 - y0) + 2 * CHIP_PAD_Y


def draw_chip(draw, x, y, label, font, box):
    """Draw one pill with its label centred strictly inside the outline."""
    x0, y0, x1, y1 = font.getbbox(label)
    # a genuinely dark pill: the banner behind it is light enough that a
    # translucent pill would leave the small bold label unreadable
    draw.rounded_rectangle([x, y, x + box[0], y + box[1]], radius=box[1] / 2,
                           fill=DARK_BG + (232,),
                           outline=(255, 255, 255, 64), width=1)
    draw.text((x + (box[0] - (x1 - x0)) / 2 - x0,
               y + (box[1] - (y1 - y0)) / 2 - y0),
              label, font=font, fill=DARK_TEXT + (255,))


def banner(app_icons) -> Image.Image:
    w, h = BANNER_W, BANNER_H
    img = diagonal_gradient((w, h), DARK_BG, BANNER_GLOW).convert("RGBA")

    # faint accent glow bleeding in from the top-right corner
    glow = soft_glow((w, h), (1212.0, 54.0), (655.0, 292.0), peak=64, falloff=3.4)
    img = Image.alpha_composite(img, Image.merge("RGBA", (
        Image.new("L", (w, h), ACCENT[0]),
        Image.new("L", (w, h), ACCENT[1]),
        Image.new("L", (w, h), ACCENT2[2]),
        glow,
    )))

    icon = app_icons[128]
    img.alpha_composite(icon, (72, (h - 128) // 2))

    d = ImageDraw.Draw(img)
    t = load_font(62, bold=True)
    sub = load_font(26)
    c = load_font(19)

    d.text((240, baseline_y(t, 150)), "GalText Extractor", font=t, fill=(255, 255, 255, 255))
    d.text((240, baseline_y(sub, 205)), BANNER_SUBTITLE, font=sub, fill=DARK_MUTED + (255,))
    d.rounded_rectangle([240, 232 - 3, 240 + 180, 232 + 3], radius=3, fill=ACCENT + (255,))
    d.text((240, baseline_y(c, 320)), BANNER_CREDIT, font=c, fill=INK_MUTED + (255,))

    # --- optional accent chips: stacked pills, right-aligned and inside margin
    fc = load_font(17, bold=True)
    chip_gap = 10
    labels = [CHIP_L + s + CHIP_R for s in BANNER_CHIPS]
    boxes = [measure_chip(d, s, fc) for s in labels]
    text_right = max(d.textlength("GalText Extractor", font=t),
                     d.textlength(BANNER_SUBTITLE, font=sub))
    limit_left = 240 + text_right + 24
    right = w - 56                            # right edge of the last pill
    x = right - max(b[0] for b in boxes)

    if x < limit_left:
        print("note: banner chips dropped (no room beside the title block)")
    else:
        y = 112
        for label, box in zip(labels, boxes):
            draw_chip(d, x, y, label, fc, box)
            y += box[1] + chip_gap
        if right > w - BANNER_MARGIN:         # belt and braces
            raise AssertionError("chip layout escaped the right margin")
    return img


# --------------------------------------------------------------------------
# 5) icon QA sheet
# --------------------------------------------------------------------------
def _paste(canvas, src, xy):
    canvas.alpha_composite(src, (int(xy[0]), int(xy[1])))


def _centered_text(d, box, text, font, fill):
    x0, y0, x1, y1 = box
    tw = d.textlength(text, font=font)
    asc, desc = font.getmetrics()
    d.text((x0 + (x1 - x0 - tw) / 2, y0 + (y1 - y0 - asc - desc) / 2),
           text, font=font, fill=fill)


def icon_preview(app_icons, toolbar) -> Image.Image:
    w, h = 1000, 460
    canvas = Image.new("RGBA", (w, h), LIGHT_BG + (255,))
    d = ImageDraw.Draw(canvas)
    fcard = load_font(15, bold=True)
    fsub = load_font(13, bold=False)

    def card(box, radius=14):
        d.rounded_rectangle(list(box), radius=radius, fill=LIGHT_SURFACE + (255,),
                            outline=LIGHT_BORDER + (255,), width=1)

    # (a) app icon at every size -------------------------------------------
    card((20, 14, 980, 168))
    cell_w = 940 / len(APP_SIZES)
    row_bottom = 136                            # labels sit under this
    for i, size in enumerate(APP_SIZES):
        cx = 20 + cell_w * (i + 0.5)
        icon = app_icons[size]
        if size > 128:
            # two 128px-plus tiles cannot sit 117px apart without touching;
            # step the oversize entries down so every cell stays separate
            icon = icon.resize((96, 96), Image.LANCZOS)
        _paste(canvas, icon, (cx - icon.width / 2, row_bottom - icon.height))
        _centered_text(d, (cx - cell_w / 2, row_bottom + 6, cx + cell_w / 2, row_bottom + 26),
                       "%dpx" % size, fsub, INK_MUTED + (255,))
    d.text((40, 24), "(a) App icon / app.ico", font=fcard, fill=INK + (255,))

    # (b) / (c) toolbar strips ---------------------------------------------
    def strip(y0, label, mode, bg):
        card((20, y0, 980, y0 + 134))
        d.rounded_rectangle([40, y0 + 34, 960, y0 + 126], radius=10, fill=bg + (255,))
        d.text((40, y0 + 10), label, font=fcard,
               fill=(INK if mode == "light" else DARK_TEXT) + (255,))
        # top row: the 20px artwork a Tk button actually renders, magnified 2x
        # so its quality is auditable; bottom row: the same glyph rasterised
        # natively at 40px, i.e. the retina version of the same icon
        cell = 56
        x0 = (1000 - 12 * cell) / 2
        y_top, y_bot = y0 + 36, y0 + 80
        d.line([(x0 + 6 * cell, y0 + 34), (x0 + 6 * cell, y0 + 126)],
               fill=LIGHT_BORDER + (255,), width=1)
        for i, name in enumerate(TOOLBAR_NAMES):
            cx = x0 + cell * (i + 0.5)
            _paste(canvas, toolbar[(mode, name, 20)].resize((32, 32), Image.NEAREST),
                   (cx - 16, y_top))
            _paste(canvas, toolbar[(mode, name, 40)], (cx - 20, y_bot))
    strip(176, LABEL_TOOLBAR_LIGHT + " / toolbar (light)", "light", LIGHT_SURFACE)
    strip(312, LABEL_TOOLBAR_DARK + " / toolbar (dark)", "dark", DARK_SURFACE)
    return canvas


# --------------------------------------------------------------------------
# build plan
# --------------------------------------------------------------------------
def build_all():
    """Return [(relative path, PIL image[, save kwargs])] -- fully in memory.

    Nothing is read back from disk: the banner and the QA sheet consume the
    images produced here, so a fresh checkout (empty assets dir) works in one
    pass and the result is independent of whatever was on disk before.
    """
    out = []
    app_icons = {}

    for size in APP_SIZES:
        img = app_icon(size)
        app_icons[size] = img
        out.append((rel(p(ASSETS, "app_%d.png" % size)), img))

    master = app_icon(256)
    out.append((rel(p(ASSETS, "app.ico")), master, {"format": "ICO", "sizes": ICO_SIZES}))

    toolbar = {}
    for mode in ("light", "dark", "accent"):
        for name in TOOLBAR_NAMES:
            small = toolbar_icon(name, ICON_STROKE[mode], 20)
            toolbar[(mode, name, 20)] = small
            out.append((rel(p(ICONS, mode, name + ".png")), small))

    # 40px variants are only needed by the preview sheet, not written to disk
    for mode in ("light", "dark"):
        for name in TOOLBAR_NAMES:
            toolbar[(mode, name, 40)] = toolbar_icon(name, ICON_STROKE[mode], 40)

    out.append((rel(p(DOCS, "banner.png")), banner(app_icons)))
    out.append((rel(p(DOCS, "icon-preview.png")), icon_preview(app_icons, toolbar)))

    # ttk 自绘主题用的复选框指示器
    out.append((rel(p(ASSETS, "check_on.png")), checkbox_icon(True)))
    out.append((rel(p(ASSETS, "check_off.png")), checkbox_icon(False)))
    return out


def ensure_dirs():
    for folder in (ASSETS, ICONS, DOCS):
        os.makedirs(folder, exist_ok=True)
    for mode in ("light", "dark", "accent"):
        os.makedirs(p(ICONS, mode), exist_ok=True)


# --------------------------------------------------------------------------
# writing / checking
# --------------------------------------------------------------------------
def save_one(path: str, img: Image.Image, kwargs=None) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if kwargs and kwargs.get("format") == "ICO":
        # Pillow downsamples the 256px RGBA master to each requested size.
        img.save(path, **kwargs)
    else:
        img.save(path, format="PNG", optimize=False)
    return os.path.getsize(path)


def check_all(expected) -> int:
    """Verify every expected output exists and is a readable image of the
    expected size.  Read-only: never redraws anything."""
    problems = []
    for path, want in expected:
        full = p(ROOT, *path.split("/"))
        if not os.path.isfile(full):
            problems.append("missing: %s" % path)
            continue
        if os.path.getsize(full) == 0:
            problems.append("empty: %s" % path)
            continue
        try:
            with Image.open(full) as im:
                im.load()
                size = im.size
        except Exception as exc:                      # unreadable / corrupt
            problems.append("invalid image: %s (%s)" % (path, exc))
            continue
        if size != want:
            problems.append("wrong size: %s is %dx%d, expected %dx%d"
                            % (path, size[0], size[1], want[0], want[1]))
    if problems:
        print("ASSETS MISSING: " + "; ".join(problems))
        return 1
    print("ASSETS OK")
    return 0


def expected_outputs():
    """The complete output manifest as (relative path, (w, h))."""
    expected = [(rel(p(ASSETS, "app_%d.png" % s)), (s, s)) for s in APP_SIZES]
    expected.append((rel(p(ASSETS, "app.ico")), (256, 256)))
    for mode in ("light", "dark", "accent"):
        for name in TOOLBAR_NAMES:
            expected.append((rel(p(ICONS, mode, name + ".png")), (20, 20)))
    expected.append((rel(p(DOCS, "banner.png")), (BANNER_W, BANNER_H)))
    expected.append((rel(p(DOCS, "icon-preview.png")), (1000, 460)))
    expected.append((rel(p(ASSETS, "check_on.png")), (CHECK_SIZE, CHECK_SIZE)))
    expected.append((rel(p(ASSETS, "check_off.png")), (CHECK_SIZE, CHECK_SIZE)))
    return expected


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv

    ensure_dirs()
    if check:
        # build_all() is never called here, so --check cannot redraw anything
        return check_all(expected_outputs())

    items = build_all()
    total = 0
    for item in items:
        path, img = item[0], item[1]
        kwargs = item[2] if len(item) > 2 else None
        n = save_one(p(ROOT, *path.split("/")), img, kwargs)
        with Image.open(p(ROOT, *path.split("/"))) as im:
            w, h = im.size
        print("%-46s %dx%d   %d" % (path, w, h, n))
        total += n

    if _FONT_STATE["degraded"]:
        print("WARNING: no system TrueType font could be loaded -- banner and "
              "label text was rendered with ImageFont.load_default() and its "
              "quality is degraded.")
    print("ASSETS OK (%d files, %d bytes)" % (len(items), total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

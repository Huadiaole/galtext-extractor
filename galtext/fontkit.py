"""Minimal TrueType/OpenType container parsing and GID-preserving glyph subsetting.

Standard library only.  Written for the galtext PDF pipeline, whose requirement is a
TrueType font that contains *only* the glyphs a game script actually uses while
keeping the original glyph IDs untouched (so composite glyph component indices
stay valid).

Public API
----------
find_font_files()        -> list[dict]   scan the system font folders
find_cjk_fonts()         -> list[dict]   same, but only CJK-capable fonts
load_font(path, index)   -> TrueTypeFont (ValueError when unparseable)
TrueTypeFont             glyph lookup, metrics, coverage, subsetting

Notes / deliberate simplifications
----------------------------------
* ``subset()`` keeps GIDs identical, therefore ``maxp.numGlyphs`` is unchanged and
  ``cmap`` can be copied verbatim.  Only ``loca``/``glyf`` are rebuilt (plus
  ``head.indexToLocFormat`` forced to 1 = long offsets).
* Glyph records are padded to a 4 byte boundary inside the new ``glyf`` table, so
  ``glyph_data()`` strips trailing NUL alignment padding to give back the logical
  glyph bytes.  The stripping is symmetric, hence byte-for-byte round-trips hold.
* CFF/OTF (``CFF ``/``CFF2``/``OTTO``) outlines are detected but cannot be
  subsetted here: ``subset()`` raises ValueError for them.
"""

from __future__ import annotations

import bisect
import logging
import os
import struct
import traceback
from pathlib import Path
from typing import Iterable

__all__ = ["find_font_files", "find_cjk_fonts", "load_font", "TrueTypeFont"]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

_TTC_TAG = b"ttcf"
_TRUE_TAGS = (0x00010000, 0x74727565)  # 0x00010000, 'true'
_OTTO_TAG = 0x4F54544F  # 'OTTO'
_CFF_TAGS = ("CFF ", "CFF2")

# tables copied verbatim into a subset (when present in the source font)
_KEEP_RAW = ("hhea", "maxp", "hmtx", "cmap", "OS/2", "post", "cvt ", "fpgm", "prep")

_ARG_1_AND_2_ARE_WORDS = 0x0001
_WE_HAVE_A_SCALE = 0x0008
_MORE_COMPONENTS = 0x0020
_WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
_WE_HAVE_A_TWO_BY_TWO = 0x0080

_MAX_TABLES = 512
_MAX_TTC_FONTS = 256
_CHECKSUM_MAGIC = 0xB1B0AFBA

#: probe used by :func:`find_cjk_fonts` (~60 common hanzi plus kana)
CJK_PROBE = (
    "的一是不了人我在有他这为之大来以个中上们到说国和地也子时道出而要于就"
    "下得可你年生自会那后能对着事其里所去行过家十用发天如然作方成者多日都"
    "あいうえおアイウエオ"
)

_VERBOSE = os.environ.get("GALTEXT_FONTKIT_VERBOSE") == "1"


# --------------------------------------------------------------------------------------
# low level byte helpers
# --------------------------------------------------------------------------------------


def _u16(buf: bytes, off: int) -> int:
    return (buf[off] << 8) | buf[off + 1]


def _i16(buf: bytes, off: int) -> int:
    return struct.unpack_from(">h", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _checksum(data: bytes) -> int:
    """Sum of big endian u32 words, mod 2**32. ``data`` must be 4 byte aligned."""
    total = 0
    n = len(data) - (len(data) % 4)
    if n:
        total = sum(struct.unpack(">%dI" % (n // 4), data[:n]))
    tail = data[n:]
    if tail:
        total += int.from_bytes(tail + b"\0" * (4 - len(tail)), "big")
    return total & 0xFFFFFFFF


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * ((-len(data)) % 4)


def _search_params(num_tables: int) -> tuple[int, int, int]:
    power = 1
    while power * 2 <= num_tables:
        power *= 2
    search_range = power * 16
    entry_selector = power.bit_length() - 1
    range_shift = num_tables * 16 - search_range
    return search_range, entry_selector, range_shift


def _is_composite(glyph: bytes) -> bool:
    """A glyph whose numberOfContours (int16 at +0) is negative is composite."""
    return len(glyph) >= 10 and _i16(glyph, 0) < 0


def _component_gids(glyph: bytes) -> list[int]:
    """Component glyph indices referenced by a composite glyph (never raises)."""
    out: list[int] = []
    try:
        if not _is_composite(glyph):
            return out
        off = 10
        for _ in range(4096):  # cycle / garbage guard
            if off + 4 > len(glyph):
                break
            flags = _u16(glyph, off)
            gid = _u16(glyph, off + 2)
            off += 4
            off += 4 if (flags & _ARG_1_AND_2_ARE_WORDS) else 2
            if flags & _WE_HAVE_A_SCALE:
                off += 2
            elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
                off += 4
            elif flags & _WE_HAVE_A_TWO_BY_TWO:
                off += 8
            out.append(gid)
            if not (flags & _MORE_COMPONENTS):
                break
    except Exception:  # pragma: no cover - defensive
        log.debug("component walk failed", exc_info=True)
    return out


# --------------------------------------------------------------------------------------
# cmap subtables
# --------------------------------------------------------------------------------------


class _CmapFmt0:
    __slots__ = ("_buf", "_base", "_limit")

    def __init__(self, buf: bytes, base: int, limit: int) -> None:
        self._buf, self._base, self._limit = buf, base, limit

    def lookup(self, cp: int) -> int:
        if cp > 0xFF:
            return 0
        pos = self._base + 6 + cp
        if pos >= self._limit or pos >= len(self._buf):
            return 0
        return self._buf[pos]


class _CmapFmt4:
    __slots__ = ("_buf", "_ends", "_starts", "_deltas", "_ranges", "_ro_pos", "_limit")

    def __init__(self, buf: bytes, base: int, length: int) -> None:
        self._buf = buf
        limit = min(len(buf), base + length if length else len(buf))
        self._limit = limit
        seg_x2 = _u16(buf, base + 6)
        seg = seg_x2 // 2
        end_pos = base + 14
        start_pos = end_pos + seg_x2 + 2
        delta_pos = start_pos + seg_x2
        ro_pos = delta_pos + seg_x2
        self._ro_pos = ro_pos
        self._ends = [_u16(buf, end_pos + 2 * i) for i in range(seg)]
        self._starts = [_u16(buf, start_pos + 2 * i) for i in range(seg)]
        self._deltas = [_i16(buf, delta_pos + 2 * i) for i in range(seg)]
        self._ranges = [_u16(buf, ro_pos + 2 * i) for i in range(seg)]

    def lookup(self, cp: int) -> int:
        i = bisect.bisect_left(self._ends, cp)
        if i >= len(self._ends) or cp < self._starts[i]:
            return 0
        ro = self._ranges[i]
        if ro == 0:
            return (cp + self._deltas[i]) & 0xFFFF
        addr = self._ro_pos + 2 * i + ro + 2 * (cp - self._starts[i])
        if addr + 2 > self._limit or addr + 2 > len(self._buf):
            return 0
        gid = _u16(self._buf, addr)
        if gid == 0:
            return 0
        return (gid + self._deltas[i]) & 0xFFFF


class _CmapFmt6:
    __slots__ = ("_buf", "_base", "_first", "_count", "_limit")

    def __init__(self, buf: bytes, base: int, length: int) -> None:
        self._buf, self._base = buf, base
        self._limit = min(len(buf), base + length if length else len(buf))
        self._first = _u16(buf, base + 6)
        self._count = _u16(buf, base + 8)

    def lookup(self, cp: int) -> int:
        delta = cp - self._first
        if delta < 0 or delta >= self._count:
            return 0
        pos = self._base + 10 + 2 * delta
        if pos + 2 > self._limit:
            return 0
        return _u16(self._buf, pos)


class _CmapFmt12:
    __slots__ = ("_starts", "_ends", "_gids")

    def __init__(self, buf: bytes, base: int, length: int) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []
        self._gids: list[int] = []
        n_groups = _u32(buf, base + 12)
        limit = min(len(buf), base + length if length else len(buf))
        pos = base + 16
        for _ in range(min(n_groups, 1 << 20)):
            if pos + 12 > limit:
                break
            start, end, gid = struct.unpack_from(">III", buf, pos)
            pos += 12
            self._starts.append(start)
            self._ends.append(end)
            self._gids.append(gid)

    def lookup(self, cp: int) -> int:
        i = bisect.bisect_right(self._starts, cp) - 1
        if i < 0 or cp > self._ends[i]:
            return 0
        gid = self._gids[i] + (cp - self._starts[i])
        return gid & 0xFFFF if gid <= 0xFFFF else gid


def _parse_cmap(buf: bytes, base: int, length: int) -> list:
    """Return cmap lookups ordered by preference (format 12, 4, 6, 0)."""
    try:
        if base + 4 > len(buf):
            return []
        n = _u16(buf, base + 2)
        candidates: list[tuple[int, object]] = []
        for i in range(min(n, 64)):
            pos = base + 4 + 8 * i
            if pos + 8 > len(buf):
                break
            pid, eid, off = struct.unpack_from(">HHI", buf, pos)
            sub = base + off
            if sub + 2 > len(buf):
                continue
            fmt = _u16(buf, sub)
            sub_len = _u16(buf, sub + 2) if fmt != 12 else _u32(buf, sub + 4)
            if fmt == 12:
                fmt_score = 4
            elif fmt == 4:
                fmt_score = 3
            elif fmt == 6:
                fmt_score = 2
            elif fmt == 0:
                fmt_score = 1
            else:
                continue
            if pid == 3 and eid in (1, 10):
                plat_score = 4
            elif pid == 0:
                plat_score = 3
            elif pid == 3 and eid == 0:
                plat_score = 2
            else:
                plat_score = 0
            if plat_score == 0:
                continue
            try:
                if fmt == 12:
                    obj = _CmapFmt12(buf, sub, sub_len)
                elif fmt == 4:
                    obj = _CmapFmt4(buf, sub, sub_len)
                elif fmt == 6:
                    obj = _CmapFmt6(buf, sub, sub_len)
                else:
                    obj = _CmapFmt0(buf, sub, sub_len)
            except Exception:
                log.debug("cmap subtable %d/%d fmt %d failed", pid, eid, fmt, exc_info=True)
                continue
            candidates.append((fmt_score * 10 + plat_score, obj))
        candidates.sort(key=lambda item: -item[0])
        return [obj for _, obj in candidates]
    except Exception:
        log.debug("cmap parse failed", exc_info=True)
        return []


def _parse_family_name(buf: bytes, base: int) -> str:
    """Read the family name (nameID 1 / 16) from a `name` table; '' on failure."""
    try:
        if base + 6 > len(buf):
            return ""
        count = _u16(buf, base + 2)
        string_offset = _u16(buf, base + 4)
        best_score = -1
        best = ""
        for i in range(min(count, 4096)):
            pos = base + 6 + 12 * i
            if pos + 12 > len(buf):
                break
            pid, eid, _lid, nid, length, off = struct.unpack_from(">HHHHHH", buf, pos)
            if nid not in (1, 16, 4):
                continue
            start = base + string_offset + off
            raw = buf[start:start + length]
            if not raw:
                continue
            try:
                if pid in (0, 3):
                    text = raw.decode("utf-16-be", "ignore")
                else:
                    text = raw.decode("latin-1", "ignore")
            except Exception:
                continue
            text = text.replace("\x00", "").strip()
            if not text:
                continue
            score = {1: 30, 16: 26, 4: 10}.get(nid, 0)
            if pid == 3 and eid in (1, 10):
                score += 5
            elif pid == 0:
                score += 4
            else:
                score += 1
            if score > best_score:
                best_score, best = score, text
        return best
    except Exception:
        log.debug("name parse failed", exc_info=True)
        return ""


# --------------------------------------------------------------------------------------
# font object
# --------------------------------------------------------------------------------------


class TrueTypeFont:
    """A parsed sfnt font (TrueType or OpenType/CFF)."""

    def __init__(self, path, face_index: int = 0, _data: bytes | None = None) -> None:
        self.path = str(path)
        self.face_index = int(face_index)
        self.units_per_em = 0
        self.num_glyphs = 0
        self.family = ""
        self.ascent = 0
        self.descent = 0
        self.line_gap = 0
        self.is_cff = False

        self._data: bytes | None = None
        self._closed = False
        self._tables: dict[str, tuple[int, int]] = {}
        self._cmaps: list = []
        self._loca: list[int] = []
        self._num_hmetrics = 0
        self._index_to_loc_format = 1

        data = _data
        if data is None:
            try:
                data = Path(path).read_bytes()
            except OSError as exc:
                raise ValueError("cannot read font file %r: %s" % (self.path, exc)) from exc
        self._parse(data)

    # -- parsing -----------------------------------------------------------------

    def _parse(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 12:
            raise ValueError("not a font: file is too short (%d bytes)" % len(data or b""))
        data = bytes(data)
        if data[:4] == _TTC_TAG:
            if len(data) < 16:
                raise ValueError("truncated TTC header")
            num_fonts = _u32(data, 8)
            if not 0 < num_fonts <= _MAX_TTC_FONTS:
                raise ValueError("bad TTC font count %d" % num_fonts)
            if self.face_index < 0 or self.face_index >= num_fonts:
                raise ValueError(
                    "TTC face index %d out of range (file has %d faces)"
                    % (self.face_index, num_fonts)
                )
            pos = 12 + 4 * self.face_index
            if pos + 4 > len(data):
                raise ValueError("truncated TTC face offset table")
            dir_offset = _u32(data, pos)
        else:
            if self.face_index not in (0, -1):
                raise ValueError("face_index %d is invalid for a single-face font file" % self.face_index)
            self.face_index = 0
            dir_offset = 0
            sfnt = _u32(data, 0)
            if sfnt not in _TRUE_TAGS and sfnt != _OTTO_TAG:
                raise ValueError("not a TrueType/OpenType font: bad sfntVersion 0x%08X" % sfnt)

        if dir_offset + 12 > len(data):
            raise ValueError("truncated table directory")
        num_tables = _u16(data, dir_offset + 4)
        if not 0 < num_tables <= _MAX_TABLES:
            raise ValueError("bad table count %d" % num_tables)
        tables: dict[str, tuple[int, int]] = {}
        for i in range(num_tables):
            rec = dir_offset + 12 + 16 * i
            if rec + 16 > len(data):
                raise ValueError("truncated table record %d" % i)
            tag = data[rec:rec + 4]
            try:
                tag = tag.decode("latin-1")
            except Exception:
                continue
            off, length = struct.unpack_from(">II", data, rec + 8)
            if off > len(data):
                continue
            tables[tag] = (off, min(length, len(data) - off))

        sfnt_version = _u32(data, dir_offset if dir_offset else 0)
        self._data = data
        self._tables = tables
        self.is_cff = bool(set(_CFF_TAGS) & set(tables)) or sfnt_version == _OTTO_TAG

        if "head" not in tables:
            raise ValueError("font has no `head` table")
        if "maxp" not in tables:
            raise ValueError("font has no `maxp` table")

        head = self._table_bytes("head")
        if len(head) < 54:
            raise ValueError("`head` table is too short (%d bytes)" % len(head))
        self.units_per_em = _u16(head, 18)
        self._index_to_loc_format = _i16(head, 50)

        maxp = self._table_bytes("maxp")
        if len(maxp) < 6:
            raise ValueError("`maxp` table is too short (%d bytes)" % len(maxp))
        self.num_glyphs = _u16(maxp, 4)

        hhea = self._table_bytes("hhea")
        if len(hhea) >= 36:
            self.ascent = _i16(hhea, 4)
            self.descent = _i16(hhea, 6)
            self.line_gap = _i16(hhea, 8)
            self._num_hmetrics = _u16(hhea, 34)

        name_rec = tables.get("name")
        if name_rec:
            self.family = _parse_family_name(data, name_rec[0])

        cmap_rec = tables.get("cmap")
        if cmap_rec:
            self._cmaps = _parse_cmap(data, cmap_rec[0], cmap_rec[1])

        self._build_loca()

    def _build_loca(self) -> None:
        self._loca = []
        if "loca" not in self._tables or "glyf" not in self._tables:
            return
        raw = self._table_bytes("loca")
        count = self.num_glyphs + 1
        try:
            if self._index_to_loc_format == 0:
                need = count * 2
                if len(raw) < need:
                    raw = raw + b"\0" * (need - len(raw))
                loca = [2 * _u16(raw, 2 * i) for i in range(count)]
            else:
                need = count * 4
                if len(raw) < need:
                    raw = raw + b"\0" * (need - len(raw))
                loca = [_u32(raw, 4 * i) for i in range(count)]
        except Exception:
            log.debug("loca parse failed for %s", self.path, exc_info=True)
            return
        glyf_len = self._tables["glyf"][1]
        loca = [min(v, glyf_len) for v in loca]
        for i in range(1, len(loca)):  # keep monotonic
            if loca[i] < loca[i - 1]:
                loca[i] = loca[i - 1]
        self._loca = loca

    def _table_bytes(self, tag: str) -> bytes:
        if self._data is None:
            return b""
        rec = self._tables.get(tag)
        if not rec:
            return b""
        off, length = rec
        return self._data[off:off + length]

    # -- public queries ----------------------------------------------------------

    def glyph_id(self, ch: str) -> int:
        """Unicode -> GID, 0 when unmapped."""
        try:
            if not ch:
                return 0
            cp = ord(ch[0])
            for cmap in self._cmaps:
                gid = cmap.lookup(cp)
                if gid:
                    return gid
            return 0
        except Exception:
            log.debug("glyph_id(%r) failed", ch, exc_info=True)
            return 0

    def has_glyph(self, ch: str) -> bool:
        return self.glyph_id(ch) != 0

    def advance_width(self, gid: int) -> int:
        """Advance width in font units; 0 when out of range."""
        try:
            gid = int(gid)
            if gid < 0 or gid >= self.num_glyphs or self._num_hmetrics <= 0:
                return 0
            hmtx = self._table_bytes("hmtx")
            if gid < self._num_hmetrics:
                off = 4 * gid
            else:
                off = 4 * (self._num_hmetrics - 1)
            if off + 2 > len(hmtx):
                return 0
            return _u16(hmtx, off)
        except Exception:
            log.debug("advance_width(%r) failed", gid, exc_info=True)
            return 0

    def coverage(self, text: str) -> tuple[int, int]:
        """(chars with a glyph, total chars)."""
        try:
            total = len(text)
            hit = sum(1 for ch in text if self.has_glyph(ch))
            return hit, total
        except Exception:
            return 0, 0

    def missing_chars(self, text: str) -> list[str]:
        """Sorted unique chars of ``text`` the font cannot render."""
        try:
            return sorted({ch for ch in text if not self.has_glyph(ch)})
        except Exception:
            return []

    def glyph_data(self, gid: int) -> bytes:
        """Raw `glyf` bytes of ``gid`` (b'' when empty/out of range).

        Trailing NUL alignment padding is stripped, which makes the value stable
        across re-serialisation (``subset()`` pads glyphs to 4 bytes).
        """
        try:
            gid = int(gid)
            if gid < 0 or gid >= self.num_glyphs or self._data is None:
                return b""
            if not self._loca or "glyf" not in self._tables:
                return b""
            start, end = self._loca[gid], self._loca[gid + 1]
            if end <= start:
                return b""
            base = self._tables["glyf"][0]
            return self._data[base + start:base + end].rstrip(b"\0")
        except Exception:
            log.debug("glyph_data(%r) failed", gid, exc_info=True)
            return b""

    def is_composite(self, gid: int) -> bool:
        return _is_composite(self.glyph_data(gid))

    def components(self, gid: int) -> list[int]:
        return _component_gids(self.glyph_data(gid))

    def _keep_closure(self, gids: Iterable[int]) -> tuple[set[int], set[int]]:
        """({0} + requested + transitive components, requested)."""
        requested: set[int] = set()
        for g in gids or ():
            try:
                g = int(g)
            except (TypeError, ValueError):
                continue
            if 0 <= g < self.num_glyphs:
                requested.add(g)
        kept = set(requested)
        kept.add(0)
        work = list(requested)
        steps = 0
        while work and steps < 4_000_000:
            steps += 1
            gid = work.pop()
            for comp in _component_gids(self.glyph_data(gid)):
                if comp in kept or not (0 <= comp < self.num_glyphs):
                    continue
                kept.add(comp)
                work.append(comp)
        return kept, requested

    # -- subsetting --------------------------------------------------------------

    def subset(self, gids: Iterable[int]) -> bytes:
        """Build a new standalone TrueType file keeping the original GIDs.

        Keeps GID 0, every requested gid and, transitively, every component glyph
        referenced by a kept composite glyph.  GIDs are *not* renumbered.
        Raises ValueError for CFF/OTF outlines or non-TrueType sources.
        """
        if self.is_cff:
            raise ValueError("CFF/OTF fonts are not supported for subsetting")
        if self._closed or self._data is None:
            raise ValueError("font is closed")
        if "glyf" not in self._tables or "loca" not in self._tables:
            raise ValueError("font has no `glyf`/`loca` table; cannot subset")
        if not self._loca:
            raise ValueError("font `loca` table could not be parsed; cannot subset")

        kept, _requested = self._keep_closure(gids)

        # glyf + loca (long format), glyph by glyph in GID order
        parts: list[bytes] = []
        offsets = [0] * (self.num_glyphs + 1)
        pos = 0
        for gid in range(self.num_glyphs):
            offsets[gid] = pos
            if gid in kept:
                glyph = self.glyph_data(gid)
                if glyph:
                    blob = _pad4(glyph)
                    parts.append(blob)
                    pos += len(blob)
        offsets[self.num_glyphs] = pos
        glyf_new = b"".join(parts)
        loca_new = struct.pack(">%dI" % (self.num_glyphs + 1), *offsets)

        head = bytearray(self._table_bytes("head"))
        if len(head) < 54:
            raise ValueError("`head` table is too short to rebuild")
        head[8:12] = b"\0\0\0\0"  # checkSumAdjustment, patched after assembly
        struct.pack_into(">h", head, 50, 1)  # indexToLocFormat = long

        tables: dict[str, bytes] = {
            "head": bytes(head),
            "loca": loca_new,
            "glyf": glyf_new,
        }
        for tag in _KEEP_RAW:
            blob = self._table_bytes(tag)
            if tag == "maxp" and not blob:
                continue
            if blob:
                tables[tag] = blob

        out = bytearray(_build_sfnt(tables))
        head_off = _find_table_offset(out, b"head")
        if head_off is not None:
            whole = _checksum(bytes(out))
            adjustment = (_CHECKSUM_MAGIC - whole) & 0xFFFFFFFF
            struct.pack_into(">I", out, head_off + 8, adjustment)
        return bytes(out)

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        try:
            self._data = None
            self._loca = []
            self._cmaps = []
            self._closed = True
        except Exception:  # pragma: no cover - defensive
            log.debug("close() failed", exc_info=True)

    def __enter__(self) -> "TrueTypeFont":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<TrueTypeFont %r face=%d family=%r glyphs=%d cff=%s>" % (
            self.path, self.face_index, self.family, self.num_glyphs, self.is_cff,
        )


# --------------------------------------------------------------------------------------
# sfnt assembly
# --------------------------------------------------------------------------------------


def _build_sfnt(tables: dict[str, bytes]) -> bytes:
    tags = sorted(tables)
    num_tables = len(tags)
    search_range, entry_selector, range_shift = _search_params(num_tables)
    out = bytearray(struct.pack(">IHHHH", 0x00010000, num_tables,
                                search_range, entry_selector, range_shift))
    offset = 12 + 16 * num_tables
    records = bytearray()
    body = bytearray()
    for tag in tags:
        raw = tables[tag]
        padded = _pad4(raw)
        records += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(padded), offset, len(raw))
        body += padded
        offset += len(padded)
    out += records
    out += body
    return bytes(out)


def _find_table_offset(sfnt: bytes, tag: bytes) -> int | None:
    try:
        num_tables = _u16(sfnt, 4)
        for i in range(num_tables):
            rec = 12 + 16 * i
            if sfnt[rec:rec + 4] == tag:
                return _u32(sfnt, rec + 8)
    except Exception:
        log.debug("table lookup failed for %r", tag, exc_info=True)
    return None


def _verify_sfnt(blob: bytes) -> None:
    """Cheap structural check used by the self-test; raises AssertionError."""
    assert _checksum(blob) == _CHECKSUM_MAGIC, "whole-file checksum adjustment is wrong"
    num_tables = _u16(blob, 4)
    assert num_tables > 0
    for i in range(num_tables):
        rec = 12 + 16 * i
        tag = blob[rec:rec + 4]
        checksum, offset, length = struct.unpack_from(">III", blob, rec + 4)
        assert offset + length <= len(blob), "table %r overruns the file" % tag
        data = blob[offset:offset + length]
        if tag == b"head":
            data = data[:8] + b"\0\0\0\0" + data[12:]
        assert _checksum(_pad4(data)) == checksum, "bad checksum for table %r" % tag


# --------------------------------------------------------------------------------------
# font discovery
# --------------------------------------------------------------------------------------


def _font_dirs() -> list[Path]:
    dirs: list[Path] = []
    win = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(Path(win) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    out: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            if d.is_dir():
                out.append(d)
        except OSError:
            continue
    return out


def _candidate_files() -> list[Path]:
    exts = {".ttf", ".ttc", ".otf", ".otc"}
    files: list[Path] = []
    seen: set[str] = set()
    for directory in _font_dirs():
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or entry.suffix.lower() not in exts:
                    continue
                key = str(entry).lower()
                if key in seen:
                    continue
                seen.add(key)
                files.append(entry)
            except OSError:
                continue
    return files


def _face_count(data: bytes) -> int:
    try:
        if data[:4] == _TTC_TAG and len(data) >= 12:
            n = _u32(data, 8)
            if 0 < n <= _MAX_TTC_FONTS:
                return n
            return 0
        return 1
    except Exception:
        return 0


def _scan(only_cjk: bool, probe: str, threshold: float) -> list[dict]:
    out: list[dict] = []
    total_probe = len(probe)
    for path in _candidate_files():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for face_index in range(_face_count(data)):
            font = None
            try:
                font = TrueTypeFont(path, face_index, _data=data)
                if only_cjk:
                    if total_probe == 0:
                        continue
                    hit, total = font.coverage(probe)
                    if total == 0 or (hit / total) < threshold:
                        continue
                out.append({
                    "path": str(path),
                    "face_index": face_index,
                    "name": font.family or path.stem,
                    "size": len(data),
                })
            except Exception as exc:
                log.debug("skip %s face %d: %s", path, face_index, exc)
            finally:
                if font is not None:
                    font.close()
    out.sort(key=lambda d: (d["name"].lower(), d["path"].lower(), d["face_index"]))
    return out


def find_font_files() -> list[dict]:
    """Scan the Windows font folders for .ttf/.ttc/.otf faces.  Never raises."""
    try:
        return _scan(False, "", 0.0)
    except Exception:
        log.warning("find_font_files failed", exc_info=True)
        return []


def find_cjk_fonts() -> list[dict]:
    """Like :func:`find_font_files` but only fonts covering >= 98% of CJK_PROBE."""
    try:
        return _scan(True, CJK_PROBE, 0.98)
    except Exception:
        log.warning("find_cjk_fonts failed", exc_info=True)
        return []


def load_font(path, face_index: int = 0) -> TrueTypeFont:
    """Load a font face.  Raises ValueError for unreadable/unparseable input."""
    try:
        return TrueTypeFont(path, face_index)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalised into ValueError by contract
        raise ValueError("cannot load font %r face %d: %s" % (str(path), face_index, exc)) from exc


# --------------------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------------------

_CANDIDATE_FONTS = ("simhei.ttf", "msyh.ttc", "Deng.ttf", "simsun.ttc")

_TEST_POOL = (
    "中文测试あアA的一是不了人我在有他这为之大来以个中上们到说国和地也子时道出"
    "而要于就下得可你年生自会那后能对着事其里所去行过家十用发天如然作方成者多"
    "濱樂醫轉體龍鬱鸞龜爐疆藏鑫"
)


def _first_existing_font() -> Path | None:
    fonts_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
    for name in _CANDIDATE_FONTS:
        candidate = fonts_dir / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    for entry in find_cjk_fonts():
        return Path(entry["path"])
    return None


def _selftest() -> int:  # noqa: C901 - linear test script
    lines: list[str] = []

    def say(msg: str) -> None:
        lines.append(msg)
        if _VERBOSE:
            print(msg, flush=True)

    import tempfile  # local import keeps module import surface tiny

    try:
        # 1. discovery -----------------------------------------------------------
        files = find_font_files()
        cjk = find_cjk_fonts()
        say("find_font_files: %d entries" % len(files))
        say("find_cjk_fonts: %d entries" % len(cjk))
        assert len(files) >= 5, "expected >=5 font files, got %d" % len(files)
        assert len(cjk) >= 1, "expected >=1 CJK font, got %d" % len(cjk)
        for entry in files[:3]:
            assert set(entry) >= {"path", "face_index", "name", "size"}, "bad entry keys"

        # 2. load chosen font ----------------------------------------------------
        target = _first_existing_font()
        assert target is not None, "no candidate system font found"
        font = load_font(str(target), 0)
        say("font: %s  family=%r  units_per_em=%d  glyphs=%d  ascent=%d descent=%d gap=%d cff=%s"
            % (target.name, font.family, font.units_per_em, font.num_glyphs,
               font.ascent, font.descent, font.line_gap, font.is_cff))
        assert font.units_per_em > 0, "units_per_em must be > 0"
        assert font.num_glyphs > 1000, "num_glyphs must be > 1000"
        assert font.ascent > 0, "ascent must be > 0"
        assert isinstance(font.family, str) and font.family.strip(), "family must be non-empty"

        # 3. glyph ids -----------------------------------------------------------
        gid_zh = font.glyph_id("中")
        gid_kana = font.glyph_id("あ")
        gid_a = font.glyph_id("A")
        say("gids: 中=%d あ=%d A=%d" % (gid_zh, gid_kana, gid_a))
        assert gid_zh != 0, "中 must be mapped"
        assert gid_kana != 0, "あ must be mapped"
        assert gid_a != 0, "A must be mapped"
        font.glyph_id("\U0001F600")  # must not raise
        assert font.glyph_id("") == 0

        # 4. metrics / coverage --------------------------------------------------
        assert font.advance_width(gid_zh) > 0, "advance width of 中 must be > 0"
        probe = "中文测试あアA"
        hit, total = font.coverage(probe)
        say("coverage(%r) = %d/%d" % (probe, hit, total))
        assert hit == total == len(probe), "probe must be fully covered"
        assert font.missing_chars(probe) == [], "probe must have no missing chars"
        assert font.coverage("") == (0, 0)
        assert font.missing_chars("") == []

        # 5. subset round trip ---------------------------------------------------
        wanted_chars: list[str] = []
        seen_chars: set[str] = set()
        for ch in _TEST_POOL:
            if ch in seen_chars:
                continue
            seen_chars.add(ch)
            if font.has_glyph(ch):
                wanted_chars.append(ch)
            if len(wanted_chars) >= 80:
                break
        assert len(wanted_chars) >= 40, "not enough mappable test chars (%d)" % len(wanted_chars)
        requested = {font.glyph_id(ch) for ch in wanted_chars}
        requested.discard(0)

        # make sure the composite closure is exercised: add a few composite gids
        composite_gids: list[int] = []
        for gid in range(font.num_glyphs - 1, 0, -1):
            if font.is_composite(gid):
                composite_gids.append(gid)
                if len(composite_gids) >= 6:
                    break
        requested |= set(composite_gids)
        say("requested gids: %d (%d composite seeds)"
            % (len(requested), len(composite_gids)))

        original_bytes = len(font._data or b"")
        blob = font.subset(sorted(requested))
        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as fh:
            fh.write(blob)
            tmp_path = fh.name
        try:
            sub = load_font(tmp_path, 0)
            say("subset: %d bytes (%.2fx smaller than %d), family=%r"
                % (len(blob), original_bytes / max(1, len(blob)), original_bytes, sub.family))
            assert sub.units_per_em == font.units_per_em, "units_per_em changed"
            assert sub.num_glyphs == font.num_glyphs, "num_glyphs changed (must be preserved)"
            assert sub.ascent == font.ascent, "ascent changed"
            assert sub.descent == font.descent, "descent changed"

            # requested glyphs survive byte for byte
            nonempty = 0
            for gid in sorted(requested):
                if font.glyph_data(gid):
                    nonempty += 1
                assert sub.glyph_data(gid) == font.glyph_data(gid), "glyph %d differs" % gid
            say("requested glyphs with bytes: %d/%d" % (nonempty, len(requested)))
            assert nonempty > 0

            # a requested gid that is not in the kept set is empty in the subset
            kept, _req = font._keep_closure(requested)
            dropped = None
            for gid in range(1, min(font.num_glyphs, 4000)):
                if gid not in kept and font.glyph_data(gid):
                    dropped = gid
                    break
            assert dropped is not None, "could not find a dropped glyph to verify"
            assert sub.glyph_data(dropped) == b"", "glyph %d should have been dropped" % dropped
            say("dropped gid %d is empty in subset: %r" % (dropped, sub.glyph_data(dropped)))

            # cmap survived
            for ch in wanted_chars:
                assert sub.glyph_id(ch) == font.glyph_id(ch), "cmap mismatch for %r" % ch
            for ch in "中文测试あアA":
                assert sub.glyph_id(ch) == font.glyph_id(ch)

            assert len(blob) * 5 <= original_bytes, (
                "subset is not >=5x smaller (%d vs %d)" % (len(blob), original_bytes)
            )
            _verify_sfnt(blob)
            say("subset sfnt structure + checksums verified")

            # 6. composite safety -------------------------------------------------
            kept_composites = 0
            checked_components = 0
            for gid in sorted(kept):
                glyph = font.glyph_data(gid)
                if not _is_composite(glyph):
                    continue
                kept_composites += 1
                for comp in _component_gids(glyph):
                    checked_components += 1
                    assert sub.glyph_data(comp) == font.glyph_data(comp), (
                        "component %d of composite %d missing/different" % (comp, gid)
                    )
                    if font.glyph_data(comp):
                        assert sub.glyph_data(comp) != b"", (
                            "component %d of composite %d is empty in subset" % (comp, gid)
                        )
            say("kept composites: %d (components checked: %d)"
                % (kept_composites, checked_components))
            # SimHei/SimSun are entirely simple glyphs, so 0 composites is legal there.
            assert kept_composites >= 1 or not composite_gids, (
                "composite seeds were requested but none survived"
            )
            sub.close()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # 6b. composite closure on a font that actually has composites -----------
        comp_font = None
        comp_seeds = list(composite_gids)
        if not comp_seeds:
            fonts_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
            for name, fi in (("msyh.ttc", 0), ("Deng.ttf", 0), ("simsun.ttc", 0)):
                cand_path = fonts_dir / name
                try:
                    if not cand_path.is_file():
                        continue
                    cand = load_font(str(cand_path), fi)
                except (ValueError, OSError):
                    continue
                seeds = [g for g in range(cand.num_glyphs - 1, 0, -1)
                         if cand.is_composite(g)][:8]
                if seeds:
                    comp_font, comp_seeds = cand, seeds
                    break
                cand.close()
        if comp_seeds:
            src = comp_font if comp_font is not None else font
            src_name = Path(src.path).name
            blob2 = src.subset(sorted(set(comp_seeds)))
            tmp2 = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as fh:
                    fh.write(blob2)
                    tmp2 = fh.name
                sub2 = load_font(tmp2, 0)
                kept2, _r2 = src._keep_closure(set(comp_seeds))
                n_comp = 0
                n_comp_checked = 0
                n_empty_refs = 0
                for gid in sorted(kept2):
                    glyph = src.glyph_data(gid)
                    if not _is_composite(glyph):
                        continue
                    n_comp += 1
                    for comp in _component_gids(glyph):
                        n_comp_checked += 1
                        assert sub2.glyph_data(comp) == src.glyph_data(comp), (
                            "component %d of composite %d differs" % (comp, gid)
                        )
                        if src.glyph_data(comp):
                            assert sub2.glyph_data(comp) != b"", (
                                "component %d of composite %d is empty in subset" % (comp, gid)
                            )
                        else:
                            n_empty_refs += 1
                say("composites in kept set of %s: %d (seed gids %s, component refs %d, "
                    "empty-source refs %d)" % (src_name, n_comp, comp_seeds[:4],
                                               n_comp_checked, n_empty_refs))
                assert n_comp >= 1, "composite closure produced no composite glyphs"
                assert n_comp_checked >= n_comp, "composites had no component references"
                assert len(blob2) * 5 <= len(src._data or b""), "composite subset not >=5x smaller"
                _verify_sfnt(blob2)
                sub2.close()
            finally:
                if tmp2:
                    try:
                        os.unlink(tmp2)
                    except OSError:
                        pass
        if comp_font is not None:
            comp_font.close()

        # 7. TTC handling --------------------------------------------------------
        ttc_entries = [e for e in files if e["path"].lower().endswith((".ttc", ".otc"))]
        cjk_paths = {e["path"].lower() for e in cjk}
        ttc_entries.sort(key=lambda e: (e["path"].lower() not in cjk_paths, e["path"].lower()))
        ttc_done = False
        for entry in ttc_entries:
            try:
                data = Path(entry["path"]).read_bytes()
            except OSError:
                continue
            if _face_count(data) < 2:
                continue
            f0 = load_font(entry["path"], 0)
            f1 = load_font(entry["path"], 1)
            try:
                say("ttc: %s  face0 family=%r glyphs=%d | face1 family=%r glyphs=%d"
                    % (Path(entry["path"]).name, f0.family, f0.num_glyphs,
                       f1.family, f1.num_glyphs))
                assert f0.num_glyphs > 1000 and f1.num_glyphs > 1000
                differs = (f0.family != f1.family) or (
                    len(f0.glyph_data(f0.glyph_id("中"))) != len(f1.glyph_data(f1.glyph_id("中")))
                ) or (f0.num_glyphs != f1.num_glyphs)
                assert differs, "TTC face 0 and face 1 look identical"
                ttc_done = True
            finally:
                f0.close()
                f1.close()
            break
        say("ttc face-1 test ran: %s" % ttc_done)

        # 8. robustness ----------------------------------------------------------
        bad = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
        try:
            bad.write(b"not a font at all")
            bad.close()
            try:
                load_font(bad.name, 0)
            except ValueError as exc:
                say("bad input raised ValueError: %s" % exc)
            else:
                raise AssertionError("load_font accepted garbage input")
        finally:
            try:
                os.unlink(bad.name)
            except OSError:
                pass

        cff_tested = False
        for entry in files:
            try:
                probe_font = load_font(entry["path"], entry["face_index"])
            except ValueError:
                continue
            try:
                if not probe_font.is_cff:
                    continue
                try:
                    probe_font.subset([1, 2, 3])
                except ValueError as exc:
                    say("CFF subset raised ValueError: %s" % exc)
                    cff_tested = True
                else:
                    raise AssertionError("subset() accepted a CFF/OTF font")
            finally:
                probe_font.close()
            break
        say("CFF subset rejection tested: %s" % cff_tested)

        find_font_files()
        find_cjk_fonts()
        font.close()
        assert font.glyph_id("中") == 0, "closed font should report no glyphs"
    except Exception:
        traceback.print_exc()
        for line in lines:
            print(line, flush=True)
        print("SELFTEST FAILED", flush=True)
        return 1

    print("SELFTEST OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

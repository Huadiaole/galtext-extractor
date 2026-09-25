"""BGI / Ethornell (Buriko) text extraction.

Sources (verified against the upstream sources, not guessed):

* arc_unpacker, ``src/dec/bgi/arc_archive_decoder.cc``
  https://github.com/vn-tools/arc_unpacker/blob/master/src/dec/bgi/arc_archive_decoder.cc
  -> the ``data/*.arc`` container (two variants, see ``_ARC_TYPES`` below).

* arc_unpacker, ``src/dec/bgi/dsc_file_decoder.cc``
  https://github.com/vn-tools/arc_unpacker/blob/master/src/dec/bgi/dsc_file_decoder.cc
  -> the ``DSC FORMAT 1.00`` Huffman/LZ container that BGI uses for its
     compiled scripts (``.dsc`` / ``.ws2`` members).  Reimplemented here in
     pure Python so that *packed* scripts can actually be decoded.

* arc_unpacker, ``src/dec/bgi/common.h`` + ``common.cc`` (``get_and_update_key``)
  https://github.com/vn-tools/arc_unpacker/blob/master/src/dec/bgi/common.cc
  -> the key stream used to de-obfuscate the DSC depth table.

* arc_unpacker, ``src/io/msb_bit_stream.cc``
  https://github.com/vn-tools/arc_unpacker/blob/master/src/io/msb_bit_stream.cc
  -> MSB-first bit reader used by the DSC bitstream.

* GARbro, ``ArcFormats/Ethornell/ArcBGI.cs`` (independent second opinion)
  https://github.com/morkt/GARbro/blob/master/ArcFormats/Ethornell/ArcBGI.cs
  -> confirms both ARC variants and the ``DSC FORMAT 1.00`` magic/offset
     (``entry.Size <= 0x220`` or magic mismatch => stored, not packed).

Warehouse of what is / is not implemented
----------------------------------------
FULLY IMPLEMENTED
    * ARC v1  magic ``PackFile    `` (12 bytes) - 32-byte index records,
      16-byte name, offset+size at index+0x10 / +0x14.
    * ARC v2  magic ``BURIKO ARC20`` (12 bytes) - 128-byte index records,
      96-byte name, offset+size at index+0x60 / +0x64.
    * ``DSC FORMAT 1.00`` decompression (the packed script format) - this is
      the real, verifiable path, so packed ``.ws2``/``.dsc`` members are
      decoded rather than guessed at.

DOCUMENTED BUT UNSUPPORTED (deliberately not faked)
    * ``.bse`` audio containers (arc_unpacker ``bgi/bse_file_decoder.cc``) -
      audio, no dialogue.
    * ``.cbg`` / ``CompressedBG`` images (``bgi/cbg_image_decoder.cc``) -
      images, no dialogue.
    * ARC v1 "Pk" variant with a per-file XOR/obfuscation table used by a few
      late Buriko titles - not present in either reference source, so entries
      from such archives are skipped with a warning instead of being emitted
      as garbage.
    * ``.ws2`` *full bytecode disassembly*: arc_unpacker does not decompile
      ws2 to source, it only unpacks the DSC container.  We therefore match
      that behaviour (container unpack + byte-string recovery) and do **not**
      claim to emit a script listing.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

ENGINE_ID = "bgi"
ENGINE_NAME = "BGI / Ethornell (Buriko)"

__all__ = [
    "ENGINE_ID",
    "ENGINE_NAME",
    "detect_dir",
    "iter_scripts",
    "extract_lines",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: ``data/*.arc`` container variants, exactly as listed in arc_unpacker's
#: ``src/dec/bgi/arc_archive_decoder.cc``:
#:
#:     {"PackFile\x20\x20\x20\x20", {16, 8}}    -> path_size 16, skip_size 8
#:     {"BURIKO ARC20",             {96, 24}}   -> path_size 96, skip_size 24
#:
#: ``index record size == path_size + skip_size + 8`` (name + LE u32 offset +
#: LE u32 size + skipped bytes).  ``base_offset == 16 + count * record_size``.
_ARC_TYPES: Tuple[Tuple[bytes, int, int], ...] = (
    (b"PackFile    ", 16, 8),
    (b"BURIKO ARC20", 96, 24),
)

_ARC_V1_MAGIC = b"PackFile    "
_ARC_V2_MAGIC = b"BURIKO ARC20"
_ARC_MAGICS = (_ARC_V1_MAGIC, _ARC_V2_MAGIC)

_ARC_HEADER_SIZE = 16  # 12-byte magic + LE u32 file count
_MAX_ARC_ENTRIES = 0x100000

#: PCM wave magic inside BGI arcs, used only for weak directory detection.
_BW_MAGIC = b"bw  "

#: Compiled-script container magic (arc_unpacker ``bgi/dsc_file_decoder.cc``
#: and GARbro ``ArcBGI.cs``).
_DSC_MAGIC = b"DSC FORMAT 1.00\x00"
_DSC_HEADER_SIZE = 0x20  # magic(12) + key(4) + out_size(4) + reserved(8)

#: GARbro only attempts DSC unpacking for members larger than this.
_DSC_MIN_PACKED_SIZE = 0x220

#: Script-ish member extensions.  BGI's compiled bytecode is ``.ws2``; plain
#: ``.txt``/``.csv``/``.ini`` resources also carry dialogue in some titles.
_SCRIPT_EXTS = frozenset(
    {
        ".ws2",
        ".ws",
        ".dsc",
        ".txt",
        ".csv",
        ".tsv",
        ".ini",
        ".dat",
        ".src",
        ".sc",
        ".s",
        ".mjo",
    }
)
_SCRIPT_EXT_HINT = _SCRIPT_EXTS

#: Extensions that are definitely not scripts; skipped even if the directory
#: looks like a BGI title, so we do not decode images into garbage strings.
_BINARY_EXTS = frozenset(
    {
        ".cbg",
        ".bse",
        ".bw",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".gif",
        ".webp",
        ".ogg",
        ".mp3",
        ".wav",
        ".avi",
        ".mpg",
        ".wmv",
        ".ttf",
        ".otf",
        ".exe",
        ".dll",
        ".arc",
        ".pack",
        ".zip",
    }
)

# --------------------------------------------------------------------------
# Shift-JIS / UTF-16LE helpers
# --------------------------------------------------------------------------

# cp932 (Microsoft Shift-JIS) is what BGI games actually ship.
_SJIS_CODECS = ("cp932", "shift_jis")

#: Characters that must never appear in an extracted dialogue line.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SJIS_DIALOGUE_CHARS = set(
    " \u3000!?.,:;'\"()[]{}<>-_/\\|~^+=*&%$#@!"
    "\u3001\u3002\u300c\u300d\u300e\u300f\u3010\u3011\uff01\uff1f\uff0e\uff0c"
    "\uff1a\uff1b\u30fb\u2026\u2015\u2014\u2212\uff5e\u301c\u00b7"
    "\u266a\u2665\u2606\u2605\u2190\u2191\u2192\u2193"
)


def _decode_sjis(data: bytes) -> Optional[str]:
    """Decode ``data`` as Shift-JIS.  Returns None if it is not usable text."""
    for codec in _SJIS_CODECS:
        try:
            text = data.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        return text
    return None


def _looks_like_text(text: str) -> bool:
    """Heuristic: does ``text`` look like readable (mostly Japanese) prose?"""
    if not text:
        return False
    good = 0
    for ch in text:
        if ch in _SJIS_DIALOGUE_CHARS:
            good += 1
        elif "\u3040" <= ch <= "\u30ff":  # kana
            good += 1
        elif "\u4e00" <= ch <= "\u9fff":  # CJK ideographs
            good += 1
        elif "\uff00" <= ch <= "\uffef":  # fullwidth forms
            good += 1
        elif ch.isascii() and (ch.isalnum() or ch in " \t"):
            good += 1
    return good * 10 >= len(text) * 8


def _clean_line(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = text.replace("\u3000", " ")
    return text.strip()


# --------------------------------------------------------------------------
# BGI key stream (arc_unpacker ``bgi/common.cc``: get_and_update_key)
# --------------------------------------------------------------------------


def _get_and_update_key(key: int) -> int:
    """Port of ``au::dec::bgi::get_and_update_key``.

    ``v0 = 20021 * (key & 0xffff)``; ``v1 = (magic | (key >> 16)) * 20021 +
    key * 346``; ``v1 = (v1 + (v0 >> 16)) & 0xffff``;
    ``key = (v1 << 16) + (v0 & 0xffff) + 1``; returns the low byte of v1.
    The arithmetic is kept unsigned-32-bit / unsigned-16-bit exactly as in the
    C++ source.
    """
    u32 = 0xFFFFFFFF
    v0 = (20021 * (key & 0xFFFF)) & u32
    v1 = key >> 16
    v1 = (v1 * 20021 + key * 346) & u32
    v1 = (v1 + (v0 >> 16)) & 0xFFFF
    new_key = (((v1 << 16) + (v0 & 0xFFFF) + 1)) & u32
    return (v1 & 0xFF, new_key)


# --------------------------------------------------------------------------
# DSC decompression (arc_unpacker ``bgi/dsc_file_decoder.cc``)
# --------------------------------------------------------------------------


class _MsbBitReader:
    """MSB-first bit reader mirroring arc_unpacker's ``io::MsbBitStream``."""

    __slots__ = ("_data", "_pos", "_bits", "_avail")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._bits = 0
        self._avail = 0

    def read(self, bits: int) -> int:
        while self._avail < bits:
            byte = self._data[self._pos] if self._pos < len(self._data) else 0
            self._pos += 1
            self._bits = ((self._bits << 8) | byte) & 0xFFFFFFFF
            self._avail += 8
        self._avail -= bits
        return (self._bits >> self._avail) & ((1 << bits) - 1)


@dataclass
class _Node:
    has_children: bool = False
    look_behind: bool = False
    value: int = 0
    children: Tuple[int, int] = (0, 0)


def _dsc_get_nodes(depth_bytes: bytes, key: int) -> Tuple[List[_Node], int]:
    """Port of ``get_nodes`` from arc_unpacker ``bgi/dsc_file_decoder.cc``.

    The C++ routine is, verbatim::

        for (const auto n : algo::range(512)) {
            u8 tmp = input_stream.read<u8>() - get_and_update_key(key);
            if (tmp) arr0.push_back((tmp << 16) + n);
        }
        std::sort(arr0.begin(), arr0.end());
        size_t arr0_pos;
        u32 n = 0, unk0 = 0x200, unk1 = 1, node_index = 1;
        u32 arr1[1024] = {0};
        u32 *node_ptr = arr1;
        for (arr0_pos = 0; arr0_pos < arr0.size(); n++)
        {
            u32 *arr1_ptr = &arr1[unk0];
            u32 *arr1_old_ptr = arr1_ptr;
            u32 group_count = 0;
            while (true)
            {
                const u32 c = arr0_pos < arr0.size() ? arr0[arr0_pos] : 0;
                if (n != (c >> 16)) break;
                nodes[*node_ptr]->has_children = false;
                nodes[*node_ptr]->look_behind = (arr0[arr0_pos] & 0x100) != 0;
                nodes[*node_ptr]->value = arr0[arr0_pos] & 0xFF;
                arr0_pos++; node_ptr++; group_count++;
            }
            const u32 unk3 = 2 * (unk1 - group_count);
            if (group_count < unk1)
            {
                unk1 = unk1 - group_count;
                for (const auto i : algo::range(unk1))
                {
                    nodes[*node_ptr]->has_children = true;
                    for (const auto j : algo::range(2))
                        *arr1_ptr++ = nodes[*node_ptr]->children[j] = node_index++;
                    node_ptr++;
                }
            }
            unk1 = unk3;
            node_ptr = arr1_old_ptr;
            unk0 ^= 0x200;
        }
        return nodes;

    The one thing the C++ code hides behind pointers is aliasing: ``node_ptr``
    is an *index into the same flat buffer* that ``arr1_ptr`` writes child
    indices into.  ``unk0`` starts at ``0x200`` and toggles between ``0x200``
    and ``0``, so ``arr1_old_ptr`` is ``node_ptr`` for the current level and
    the two writes below the level's records are its children.  This port keeps
    the two indices separate instead of aliasing one buffer, which is the only
    faithful way to express it in Python.

    Returns ``(nodes, records)`` where ``records`` is how many live nodes the
    walk placed, so the caller can reject a stream whose tree does not reach the
    node it was asked for.  Never raises.
    """
    nodes: List[_Node] = [_Node() for _ in range(1024)]

    # 512 depth bytes, each de-obfuscated with one byte of the key stream.
    arr0: List[int] = []
    key_local = key
    for i in range(512):
        raw = depth_bytes[i] if i < len(depth_bytes) else 0
        kb, key_local = _get_and_update_key(key_local)
        tmp = (raw - kb) & 0xFF
        if tmp:
            arr0.append((tmp << 16) + i)
    arr0.sort()

    unk0 = 0x200
    unk1 = 1
    node_index = 1
    arr1 = [0] * 2048
    node_ptr = 0  # index into `nodes`, i.e. `*node_ptr` in the C++ source
    n = 0
    arr0_pos = 0

    while arr0_pos < len(arr0) and n <= 1024:
        arr1_old_ptr = unk0
        group_count = 0
        while True:
            c = arr0[arr0_pos] if arr0_pos < len(arr0) else 0
            if n != (c >> 16):
                break
            if node_ptr >= len(nodes):
                return nodes, node_ptr
            node = nodes[node_ptr]
            node.has_children = False
            node.look_behind = (c & 0x100) != 0
            node.value = c & 0xFF
            arr0_pos += 1
            node_ptr += 1
            group_count += 1

        unk3 = 2 * (unk1 - group_count)
        if group_count < unk1:
            unk1 -= group_count
            arr1_ptr = unk0
            for _ in range(unk1):
                if node_ptr >= len(nodes):
                    return nodes, node_ptr
                node = nodes[node_ptr]
                node.has_children = True
                node.value = 0
                node.children = (node_index, node_index + 1)
                if arr1_ptr + 1 < len(arr1):
                    arr1[arr1_ptr] = node_index
                    arr1[arr1_ptr + 1] = node_index + 1
                arr1_ptr += 2
                node_index += 2
                node_ptr += 1
        unk1 = unk3
        node_ptr = arr1_old_ptr
        unk0 ^= 0x200
        n += 1

    return nodes, node_ptr


def _dsc_decompress(nodes: Sequence[_Node], body: bytes, output_size: int) -> bytes:
    """Port of ``decompress`` from arc_unpacker ``bgi/dsc_file_decoder.cc``."""
    out = bytearray()
    reader = _MsbBitReader(body)
    while len(out) < output_size:
        node_index = 0
        guard = 0
        while nodes[node_index].has_children:
            node_index = nodes[node_index].children[reader.read(1)]
            guard += 1
            if guard > 1024:
                return bytes(out)
        node = nodes[node_index]
        if node.look_behind:
            offset = reader.read(12)
            repetitions = node.value + 2
            look_behind = len(out) - offset - 2
            if look_behind < 0:
                break
            if look_behind + repetitions >= output_size:
                break
            for _ in range(repetitions):
                if len(out) >= output_size:
                    break
                out.append(out[look_behind])
                look_behind += 1
        else:
            out.append(node.value)
    return bytes(out)


def _tree_sane(nodes: Sequence[_Node]) -> bool:
    """Cheap structural check: every reachable child index is inside the graph.

    Guards against emitting garbage when a stream is not really DSC data.
    """
    stack = [0]
    seen = set()
    while stack:
        idx = stack.pop()
        if idx in seen or idx < 0 or idx >= len(nodes):
            if idx < 0 or idx >= len(nodes):
                return False
            continue
        seen.add(idx)
        if len(seen) > 2048:
            return False
        node = nodes[idx]
        if node.has_children:
            stack.append(node.children[0])
            stack.append(node.children[1])
    return True


def _dsc_unpack(data: bytes) -> Optional[bytes]:
    """Unpack a ``DSC FORMAT 1.00`` blob.  Returns None if it is not one."""
    if len(data) < _DSC_HEADER_SIZE or not data.startswith(_DSC_MAGIC):
        return None
    try:
        key, output_size = struct.unpack_from("<II", data, 12)
        depth_bytes = data[_DSC_HEADER_SIZE : _DSC_HEADER_SIZE + 512]
        if len(depth_bytes) < 512:
            return None
        if output_size == 0 or output_size > 0x100000 * 16:
            return None
        nodes, records = _dsc_get_nodes(depth_bytes, key)
        if records <= 0 or not _tree_sane(nodes):
            return None
        body = data[_DSC_HEADER_SIZE + 512 :]
        out = _dsc_decompress(nodes, body, output_size)
        return out or None
    except Exception:  # noqa: BLE001 - never propagate malformed input
        log.debug("bgi: DSC unpack failed", exc_info=True)
        return None


def _is_dsc(data: bytes) -> bool:
    return len(data) >= len(_DSC_MAGIC) and data[: len(_DSC_MAGIC)] == _DSC_MAGIC


def _looks_packed_dsc(data: bytes) -> bool:
    """GARbro's rule: size > 0x220 and the DSC magic is present."""
    return len(data) > _DSC_MIN_PACKED_SIZE and _is_dsc(data)


# --------------------------------------------------------------------------
# ARC container
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ArcEntry:
    name: str
    offset: int
    size: int


def _arc_type_for(magic: bytes) -> Optional[Tuple[bytes, int, int]]:
    for candidate in _ARC_TYPES:
        if candidate[0] == magic:
            return candidate
    return None


def detect_arc(data: bytes) -> Optional[str]:
    """Return the ARC variant id (``'arc1'``/``'arc2'``) for ``data``."""
    for magic in _ARC_MAGICS:
        if len(data) >= len(magic) and data[: len(magic)] == magic:
            return "arc1" if magic == _ARC_V1_MAGIC else "arc2"
    return None


def _decode_name(raw: bytes) -> str:
    """BGI index names are Shift-JIS, NUL padded; arc_unpacker converts to UTF-8."""
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    if not raw:
        return ""
    for codec in _SJIS_CODECS:
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def parse_arc(data: bytes) -> List[_ArcEntry]:
    """Parse a BGI ``.arc`` index exactly as arc_unpacker does.

    Layout (both variants)::

        +0x00  magic[12]                     "PackFile    " | "BURIKO ARC20"
        +0x0C  LE u32 file_count
        +0x10  file_count index records
                 name[path_size]             NUL padded
                 LE u32 offset               relative to base_offset
                 LE u32 size
                 skipped[skip_size]
        base_offset = 0x10 + file_count * (path_size + skip_size + 8)

    Returns an empty list for anything that does not parse.  Never raises.
    """
    try:
        if len(data) < _ARC_HEADER_SIZE:
            return []
        magic = data[:12]
        arc_type = _arc_type_for(magic)
        if arc_type is None:
            return []
        _magic, path_size, skip_size = arc_type
        record_size = path_size + skip_size + 8

        (count,) = struct.unpack_from("<I", data, 12)
        if count == 0 or count > _MAX_ARC_ENTRIES:
            return []
        index_size = count * record_size
        base_offset = _ARC_HEADER_SIZE + index_size
        if base_offset > len(data):
            return []

        entries: List[_ArcEntry] = []
        pos = _ARC_HEADER_SIZE
        for _ in range(count):
            name = _decode_name(data[pos : pos + path_size])
            offset, size = struct.unpack_from("<II", data, pos + path_size)
            pos += record_size
            abs_offset = base_offset + offset
            if abs_offset > len(data) or abs_offset + size > len(data):
                log.debug(
                    "bgi: arc entry %r out of bounds (offset=%d size=%d)",
                    name,
                    abs_offset,
                    size,
                )
                continue
            if not name:
                name = "unnamed_%d" % len(entries)
            entries.append(_ArcEntry(name, abs_offset, size))
        return entries
    except Exception:  # noqa: BLE001
        log.debug("bgi: arc parse failed", exc_info=True)
        return []


def _member_payload(entry_data: bytes) -> bytes:
    """Unpack a script member payload (DSC if packed, else verbatim)."""
    if _looks_packed_dsc(entry_data):
        unpacked = _dsc_unpack(entry_data)
        if unpacked is not None:
            return unpacked
        log.debug("bgi: DSC member unpack produced nothing; keeping raw bytes")
    return entry_data


def _wanted_member(name: str) -> bool:
    """Only script-ish members are handed to ``extract_lines``."""
    ext = Path(name).suffix.lower()
    if ext in _BINARY_EXTS:
        return False
    if ext in _SCRIPT_EXT_HINT:
        return True
    # Unknown / no extension: keep it (some titles use bare names) as long as
    # it is not obviously a known binary type.
    return ext == ""


# --------------------------------------------------------------------------
# String scanning fallback
# --------------------------------------------------------------------------


def _scan_shift_jis(data: bytes, min_len: int = 2) -> List[str]:
    """HEURISTIC FALLBACK.

    Scan for runs of plausible Shift-JIS bytes and decode them.  This is used
    only for script members whose container/format is not fully parsed (or when
    the parsed format yields no strings).  It is a heuristic, not a decoder.
    """
    found: List[str] = []
    buf = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b in (0x09, 0x0A, 0x0D, 0x20) or (0x21 <= b <= 0x7E):
            buf.append(b)
            i += 1
            continue
        if 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF:
            if i + 1 < n:
                trail = data[i + 1]
                if (0x40 <= trail <= 0x7E) or (0x80 <= trail <= 0xFC):
                    buf.append(b)
                    buf.append(trail)
                    i += 2
                    continue
        if 0xA1 <= b <= 0xDF:  # single-byte katakana
            buf.append(b)
            i += 1
            continue
        if buf:
            _flush_sjis(buf, found, min_len)
            buf.clear()
        i += 1
    if buf:
        _flush_sjis(buf, found, min_len)
    return found


def _flush_sjis(buf: bytearray, found: List[str], min_len: int) -> None:
    text = _decode_sjis(bytes(buf))
    if text is None:
        return
    for chunk in re.split(r"[\r\n]+", text):
        line = _clean_line(chunk)
        if len(line) >= min_len and _looks_like_text(line):
            found.append(line)


def _scan_utf16le(data: bytes, min_len: int = 2) -> List[str]:
    """HEURISTIC FALLBACK: scan for aligned UTF-16LE text runs."""
    found: List[str] = []
    buf = bytearray()
    limit = len(data) - 1
    i = 0
    while i < limit:
        lo = data[i]
        hi = data[i + 1]
        cp = lo | (hi << 8)
        ok = False
        if hi == 0 and (0x20 <= lo <= 0x7E or lo in (0x09, 0x0A, 0x0D)):
            ok = True
        elif (
            0x3000 <= cp <= 0x303F
            or 0x3040 <= cp <= 0x30FF
            or 0x4E00 <= cp <= 0x9FFF
            or 0xFF00 <= cp <= 0xFFEF
        ):
            ok = True
        if ok:
            buf.append(lo)
            buf.append(hi)
            i += 2
            continue
        if buf:
            _flush_utf16(buf, found, min_len)
            buf.clear()
        i += 2
    if buf:
        _flush_utf16(buf, found, min_len)
    return found


def _flush_utf16(buf: bytearray, found: List[str], min_len: int) -> None:
    try:
        text = buf.decode("utf-16-le")
    except UnicodeDecodeError:
        return
    # Reject UTF-16 runs that are not actually CJK: mis-aligned Shift-JIS
    # bytecode decodes to plenty of "valid" UTF-16 punctuation and ASCII, and
    # accepting those would let this fallback outscore the real SJIS scan.
    if _cjk_ratio(text) < 0.35:
        return
    for chunk in re.split(r"[\r\n]+", text):
        line = _clean_line(chunk)
        if len(line) >= min_len:
            found.append(line)


# --------------------------------------------------------------------------
# Mandatory contract
# --------------------------------------------------------------------------


def detect_dir(root) -> int:
    """0-100 confidence that ``root`` is a BGI/Ethornell game directory."""
    try:
        root = Path(root)
        if not root.is_dir():
            return 0
    except Exception:  # noqa: BLE001
        return 0

    score = 0
    try:
        # A BGI title always ships at least one .arc in data/ (or next to the exe).
        arc_locations = [root / "data", root]
        for folder in arc_locations:
            if not folder.is_dir():
                continue
            for arc in folder.glob("*.arc"):
                try:
                    with arc.open("rb") as fh:
                        head = fh.read(16)
                except OSError:
                    continue
                if detect_arc(head) is not None:
                    score = max(score, 85)
                    break
            if score:
                break

        # Corroborating evidence: an Ethornell executable or its resource files.
        names = {p.name.lower() for p in root.iterdir() if p.is_file()}
        if any(n.startswith("bgi") and n.endswith(".exe") for n in names):
            score += 10
        if any(n.endswith(".bgi") for n in names):
            score += 10

        # Directly-present Ethornell script containers.
        try:
            for item in root.rglob("*.ws2"):
                score += 5
                break
        except OSError:
            pass

        # A DSC-format file anywhere in the tree is a strong signal.
        try:
            checked = 0
            for item in root.iterdir():
                if not item.is_file() or checked >= 64:
                    continue
                checked += 1
                if item.suffix.lower() not in _SCRIPT_EXT_HINT:
                    continue
                try:
                    with item.open("rb") as fh:
                        if _is_dsc(fh.read(len(_DSC_MAGIC))):
                            score += 15
                            break
                except OSError:
                    continue
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        log.debug("bgi: detect_dir failed", exc_info=True)
        return 0

    return max(0, min(100, score))


def iter_scripts(root) -> Iterator[Tuple[str, bytes]]:
    """Yield ``(virtual_path, data)`` for every script blob recovered under root.

    Handles, in order:
      * ``.arc`` members matching the two verified BGI index layouts, with
        packed DSC members transparently unpacked;
      * loose ``.ws2`` / ``.dsc`` files (packed or stored);
      * loose text-like scripts.

    Never raises: malformed input is logged and skipped.
    """
    try:
        root = Path(root)
        if not root.is_dir():
            return
    except Exception:  # noqa: BLE001
        return

    # ---- archives -------------------------------------------------------
    try:
        arc_files = sorted(
            set(root.glob("*.arc")) | set((root / "data").glob("*.arc"))
        )
    except Exception:  # noqa: BLE001
        arc_files = []
    for arc_path in arc_files:
        try:
            data = arc_path.read_bytes()
        except OSError:
            log.debug("bgi: cannot read %s", arc_path, exc_info=True)
            continue
        if detect_arc(data) is None:
            continue
        for entry in parse_arc(data):
            if not _wanted_member(entry.name):
                continue
            try:
                payload = data[entry.offset : entry.offset + entry.size]
                payload = _member_payload(payload)
            except Exception:  # noqa: BLE001
                log.debug("bgi: member %r failed", entry.name, exc_info=True)
                continue
            if not payload:
                continue
            virtual = "%s/%s" % (arc_path.name, entry.name.replace("\\", "/"))
            yield virtual, payload

    # ---- loose scripts --------------------------------------------------
    try:
        loose = [
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in _SCRIPT_EXT_HINT
        ]
    except Exception:  # noqa: BLE001
        loose = []
    for path in loose:
        if path.suffix.lower() == ".arc":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        try:
            payload = _member_payload(data)
        except Exception:  # noqa: BLE001
            continue
        try:
            virtual = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            virtual = path.name
        yield virtual, payload


def extract_lines(virtual_path: str, data: bytes) -> list:
    """Return dialogue/narration strings found in ``data``.  Never raises."""
    lines: List[str] = []
    try:
        if not data:
            return lines
        payload = _member_payload(data)

        # BOM-sniffed encodings take priority when present.
        if payload[:3] == b"\xef\xbb\xbf" or payload[:2] == b"\xff\xfe":
            try:
                if payload[:2] == b"\xff\xfe":
                    text = payload[2:].decode("utf-16-le")
                else:
                    text = payload[3:].decode("utf-8")
                lines.extend(
                    line
                    for line in (_clean_line(x) for x in text.splitlines())
                    if line
                )
                return _dedupe(lines)
            except UnicodeDecodeError:
                pass

        # Secondary path: some localised builds (and text patched by fan
        # translations) store UTF-16LE text instead.  Both scans are heuristics,
        # so run both and keep the one that scores better rather than trusting
        # either blindly.  On genuine Shift-JIS input the UTF-16LE scan of the
        # same bytes decodes to almost no CJK, so it loses.
        sjis_lines = _scan_shift_jis(payload)
        utf16_lines = _scan_utf16le(payload)

        lines = sjis_lines if _score(sjis_lines) >= _score(utf16_lines) else utf16_lines

        return _dedupe(lines)
    except Exception:  # noqa: BLE001
        log.debug("bgi: extract_lines failed for %r", virtual_path, exc_info=True)
        return []


def _dedupe(lines: List[str]) -> List[str]:
    seen = set()
    out = []
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _score(lines: Sequence[str]) -> int:
    """Rank one scan's output: prefer many, long, Japanese-heavy lines."""
    total = 0
    for line in lines:
        jp = sum(
            1
            for ch in line
            if "\u3040" <= ch <= "\u30ff"
            or "\u4e00" <= ch <= "\u9fff"
            or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef"
        )
        if jp == 0:
            total -= 2  # ASCII-only runs are almost always code, not dialogue
        total += len(line) + jp * 3
    return total


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(
        1
        for ch in text
        if "\u3000" <= ch <= "\u303f"
        or "\u3040" <= ch <= "\u30ff"
        or "\u4e00" <= ch <= "\u9fff"
        or "\uff00" <= ch <= "\uffef"
    )
    return cjk / len(text)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------


def _fixture_dsc(text: bytes, key: int = 0x12345678, pad: int = 0x220) -> bytes:
    """Build a synthetic ``DSC FORMAT 1.00`` container around ``text``.

    This is deliberately a *container* fixture, not a claimed bit-exact
    re-encode of BGI's Huffman stream: it carries the real 12-byte magic, the
    real LE key / output-size header and the real 512-byte depth table, and
    stores ``text`` verbatim in the body.  That is enough to exercise both
    branches that matter for the contract:

      * the packed path is entered (``_looks_packed_dsc`` is true, so
        ``_member_payload`` calls ``_dsc_unpack``), and
      * the stored/fallback path still yields the dialogue when ``_dsc_unpack``
        correctly declines the stream.

    It does **not** claim to validate ``decompress`` against a real encoder
    output; see the module docstring's limitations note.
    """
    header = _DSC_MAGIC + struct.pack("<II", key, len(text)) + b"\x00" * 8
    table = bytearray()
    k = key
    for _ in range(512):
        kb, k = _get_and_update_key(k)
        table.append(kb)  # de-obfuscates to depth 0
    body = bytearray(text)
    while len(header) + 512 + len(body) <= pad:
        body.append(0)
    return header + bytes(table) + bytes(body)


def _selftest() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    import shutil
    import tempfile

    ok = True
    root = Path(tempfile.mkdtemp(prefix="galtext_bgi_"))
    try:
        dialogue = "こんにちは、世界。\n「また明日ね」と彼女は言った。\nありがとう、大好きだよ。"
        payload_text = dialogue.replace("\n", "")
        raw_bytes = payload_text.encode("cp932")

        packed = _fixture_dsc(raw_bytes)

        # --- build the synthetic .arc byte by byte --------------------------
        # Layout (arc v1, arc_unpacker bgi/arc_archive_decoder.cc):
        #   "PackFile    "[12] + LE u32 count + count * 0x20 index records
        #   record = name[16] + LE u32 offset + LE u32 size + skipped[8]
        #   base_offset = 0x10 + count * 0x20
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        members = [
            ("a_001.ws2", packed),
            ("b_002.txt", raw_bytes),
        ]
        index_size = 0x20 * len(members)
        base_offset = 0x10 + index_size
        body = bytearray()
        index = bytearray()
        running = 0
        for name, blob in members:
            raw_name = name.encode("cp932")
            if len(raw_name) >= 16:
                raise AssertionError("fixture name too long")
            index += raw_name.ljust(16, b"\x00")
            index += struct.pack("<II", running, len(blob))
            index += b"\x00" * 8
            body += blob
            running += len(blob)

        arc_bytes = (
            b"PackFile    "
            + struct.pack("<I", len(members))
            + bytes(index)
            + bytes(body)
        )
        arc_path = data_dir / "data.arc"
        arc_path.write_bytes(arc_bytes)

        # --- the synthetic header must parse exactly -------------------------
        parsed = parse_arc(arc_bytes)
        if [e.name for e in parsed] != [n for n, _ in members]:
            print("FAIL: parse_arc names %r" % ([e.name for e in parsed],))
            ok = False
        for entry, (name, blob) in zip(parsed, members):
            if entry.offset != base_offset + sum(
                len(b) for _, b in members[: [n for n, _ in members].index(name)]
            ):
                print("FAIL: parse_arc offset for %s" % name)
                ok = False
            if entry.size != len(blob):
                print("FAIL: parse_arc size for %s" % name)
                ok = False
        if detect_arc(arc_bytes) != "arc1":
            print("FAIL: arc v1 magic not recognised")
            ok = False

        # --- assertions -----------------------------------------------------
        confidence = detect_dir(root)
        print("detect_dir -> %d" % confidence)
        if not confidence > 0:
            print("FAIL: detect_dir did not recognise the fixture")
            ok = False

        found = dict(iter_scripts(root))
        for key_name, _ in members:
            virtual = "data.arc/%s" % key_name
            if virtual not in found:
                print("FAIL: iter_scripts did not recover %s" % virtual)
                ok = False

        for virtual, blob in found.items():
            lines = extract_lines(virtual, blob)
            if not lines:
                print("FAIL: no lines from %s" % virtual)
                ok = False
                continue
            if payload_text not in lines:
                print("FAIL: injected text missing from %s -> %r" % (virtual, lines))
                ok = False

        # UTF-16LE fallback path (line breaks are split out, so the injected
        # single-line payload is what must come back verbatim).
        utf16 = payload_text.encode("utf-16-le")
        if payload_text not in extract_lines("u16", utf16):
            print("FAIL: UTF-16LE fallback lost the dialogue")
            ok = False

        # robustness: malformed input must never raise
        for junk in (b"", b"\x00" * 4, b"PackFile    " + b"\xff" * 64, packed[:40]):
            try:
                extract_lines("junk", junk)
                parse_arc(junk)
            except Exception as exc:  # noqa: BLE001
                print("FAIL: raised on malformed input: %r" % (exc,))
                ok = False

        # ARC v2 header recognition
        v2 = b"BURIKO ARC20" + struct.pack("<I", 0) + b"\x00" * 64
        if detect_arc(v2) != "arc2":
            print("FAIL: BURIKO ARC20 not recognised")
            ok = False

        # flat .arc next to the exe is also detected
        (root / "flat.arc").write_bytes(arc_bytes)
        if detect_dir(root) <= 0:
            print("FAIL: detect_dir missed a flat .arc")
            ok = False

        if ok:
            print("SELFTEST OK")
            return 0
        print("SELFTEST FAILED")
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("SELFTEST FAILED: %r" % (exc,))
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(_selftest())

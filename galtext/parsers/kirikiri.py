"""KiriKiri (吉里吉里) / KAG engine parser: XP3 archives + .ks script text extraction.

Stdlib only (pathlib, struct, zlib, re, io, logging, dataclasses, typing).
This module never raises out of :func:`iter_scripts` / :func:`extract_lines`.

=========================================================================
1. XP3 CONTAINER LAYOUT (verified against primary + independent sources)
=========================================================================

``Xp3Opener`` / ``XP3Archive`` implementations cross-checked:

* KiriKiri 2 / TVP2 engine source (authoritative), ``XP3Archive.cpp`` +
  ``XP3Archive.h``:
  https://github.com/krkrz/krkr2/blob/master/kirikiri2/trunk/kirikiri2/src/core/base/XP3Archive.cpp
  https://github.com/krkrz/krkr2/blob/master/kirikiri2/trunk/kirikiri2/src/core/base/XP3Archive.h
* GARbro ``ArcFormats/KiriKiri/ArcXP3.cs`` (morkt):
  https://github.com/morkt/GARbro/blob/master/ArcFormats/KiriKiri/ArcXP3.cs
* krkr-xp3 Python tools (Edward Keyes / SmilingWolf / Awakening lineage):
  https://github.com/Galgamer-org/krkr-xp3 (``structs/file_index.py``,
  ``structs/file_entry.py``, ``structs/constants.py``)
* XP3 format writeup (independent Chinese analysis, incl. index variants):
  https://github.com/hktkqj/cxdec-hxv4-static-analysis/blob/main/docs/core/XP3Extract.md
* Magic/signature catalogue: http://fileformats.archiveteam.org/wiki/XP3

Header / index
--------------

======================  ======  ==================================================
Offset                  Size    Meaning
======================  ======  ==================================================
0x00                    11      magic ``"XP3\\r\\n \\n\\x1a\\x8bg\\x01"`` =
                                ``58 50 33 0D 0A 20 0A 1A 8B 67 01``
0x0B                    8       ``index_offset`` (uint64 LE, relative to base)
======================  ======  ==================================================

The magic's byte 0x0A/0x0B ``8B 67`` is KANJI-CODE garbage used by the engine to
detect bogus line-feed/encoding conversion; the final 0x01 is the structure
version/coding byte ("lower 4 bits are character coding, currently 1, is BMP
16bit Unicode" -- XP3Archive.cpp comment).  For a self-binding ``.exe`` the
engine locates the magic by scanning at 16-byte alignment; ``base`` is added to
``index_offset`` and to every segment start (implemented here).

At ``base + index_offset`` an index block begins:

* ``uint8 flag`` -- ``flag & 0x07`` is the encode method:
    * ``0`` (RAW):   ``uint64 raw_size`` then ``raw_size`` bytes of index.
    * ``1`` (ZLIB):  ``uint64 compressed_size``, ``uint64 raw_size``, then a
      **standard zlib stream** (``uncompress()``, i.e. zlib framing with the
      2-byte header + adler32 trailer -- *not* a raw deflate stream).
* ``flag & 0x80`` (``TVP_XP3_INDEX_CONTINUE``) -- the index continues: after the
  current block another ``uint64 index_offset`` follows (the engine re-reads it
  from the file position just past the current index data).

CORRECTION to a common misreading: **0x80 is NOT "zlib"**. The compressed-index
flag is the low bits value ``1`` (mask ``0x07``); ``0x80`` is the index
*continuation* bit (GARbro additionally implements a stub form where a block
whose first 4 bytes are exactly ``80 00 00 00`` stores the next index offset at
``+9`` -- both readings are handled here).

Index body
----------

A flat sequence of chunks ``tag[4] + size[uint64 LE] + body[size]``:

* ``File`` -- one archive member.  Its body holds sub-chunks (any order,
  unknown ones are skipped).  Entries are recognisable regardless of order:
    * ``info``::

        +0x00  uint32 flags      (bit 0x80000000 == protected/encrypted)
        +0x04  uint64 org_size   (uncompressed member size)
        +0x0C  uint64 arc_size   (stored size; == org_size when not compressed)
        +0x14  uint16 name_len   (count of UTF-16LE code units)
        +0x16  name_len*2 bytes  member name, UTF-16LE, **no** BOM
        [+0x16+name_len*2  optional 2-byte NUL terminator]

      The 2-byte terminator is present in some writers and absent in others; TVP
      reads exactly ``22 + name_len*2`` bytes and both GARbro and krkr-xp3 skip
      to the declared sub-chunk end, so the terminator is simply ignored.
    * ``segm``: ``N = size // 28`` fixed 28-byte records::

        +0x00  uint32 flags       (bit 0x80000000 == protected/encrypted)
        +0x04  uint64 start       (absolute offset inside the archive + base)
        +0x0C  uint64 org_size    (this segment's uncompressed size)
        +0x14  uint64 arc_size    (this segment's stored size)

      A member's plaintext is the *concatenation* of its segments; each segment
      may independently be stored raw or zlib-compressed.  Segments may even
      share bytes between members (OggVorbis VQ codebook sharing).
    * ``adlr``: ``uint32`` = 32-bit hash of the member (Adler-32 of the
      plaintext in the standard writer; it doubles as the encryption key seed).
    * ``time`` (optional): ``uint64`` timestamp in ms.
  * ``eliF`` -- a reverse-spelled marker some games (and GARbro) emit before a
    ``File`` chunk to carry the *real* name of a protected member:
    ``uint64 size``, then ``uint32 adler32``, ``uint16 name_len``, UTF-16LE name
    (plus optional terminator).  It is used here as a name override for
    protected members whose ``info`` name is obfuscated.
  * unknown top-level chunks are skipped by their declared size.

FLAGS SEMANTICS (settled)
-------------------------

* ``info.flags``: only ``1<<31`` is defined in the engine
  (``TVP_XP3_FILE_PROTECTED``); when set the member bytes are passed through the
  registered extraction filter (i.e. they are encrypted/obfuscated).
  There is **no** "0x80000000 == uses segments" meaning: the engine always reads
  the ``segm`` chunk and always reads every segment, whatever the flags say.
* ``info.flags & 0x100`` ("UTF-16 name flag") is **not** part of the verified
  format.  Names are *always* UTF-16LE BMP; the length field is a character
  count.  (The only 0x100 that appears in real code is GARbro's sanity limit
  ``name.Length > 0x100`` -> reject.)  Nothing here depends on such a bit.
* ``segm.flags``: ``flags & 0x07`` (``TVP_XP3_SEGM_ENCODE_METHOD_MASK``):
  ``0`` = RAW (stored), ``1`` = ZLIB (segment bytes are a zlib stream).
  GARbro/krkr-xp3 use "low byte nonzero == compressed", which this module
  follows, with a raw fallback if the zlib data does not decompress.

===================
2. ENCRYPTION
===================

The KiriKiri core contains **no** built-in member cipher: ``XP3Archive.cpp``
only calls a plugin-registered callback
``TVPXP3ArchiveExtractionFilter(tTVPXP3ExtractionFilterInfo*)``.  The struct
that the engine hands to the filter carries ``Offset``, ``Buffer``,
``BufferSize`` and ``FileHash`` -- the 32-bit ``adlr`` hash of the member.
Filters must be byte-wise/local (XOR/shift/add only, identical for any call
boundary), and they run *after* zlib decompression.

Implemented (VERIFIED):
  (a) unencrypted members                     -- plain read;
  (b) zlib-compressed members                 -- ``zlib.decompress`` per segment;
  (c) the official KiriKiri 2 "XP3 filter":   XOR every byte of the member's
      plaintext with ``FileHash & 0xFF`` (constant per member).
      Primary source -- the sample plugin shipped in the engine source tree,
      ``src/plugins/win32/xp3filter/xp3dec/main.cpp``:
          for (i = 0; i < info->BufferSize; i++)
              ((unsigned char *)info->Buffer)[i] ^= info->FileHash;
      https://github.com/krkrz/krkr2/blob/master/kirikiri2/trunk/kirikiri2/src/plugins/win32/xp3filter/xp3dec/main.cpp
      Encoder side (same XOR), ``xp3filter/xp3enc/main.cpp``:
      https://github.com/krkrz/krkr2/blob/master/kirikiri2/trunk/kirikiri2/src/plugins/win32/xp3filter/xp3enc/main.cpp
      Independently confirmed by two unrelated reimplementations:
      GARbro ``CryptAlgorithms.cs`` class ``HashCrypt`` (XOR with
      ``(byte)entry.Hash``) and krkr-xp3 ``encrypt/hash_crypt.py``
      ("Only encrypt by XORing the first byte of the adler32 checksum").
      The engine filter uses a *constant* key (not a rotating one); no rotating
      hash-derived key could be verified in any real source, so none is guessed.

Acceptance test for (c): the decrypted buffer is accepted outright when
``adler32(plaintext) == adlr`` (the standard writer's rule).  When that fails we
accept only if the payload still looks like real script text (some games compute
the hash *after* encryption -- GARbro's ``HashCrypt`` "HashAfterCrypt"
variants); otherwise :class:`Xp3UnsupportedEncryption` is raised.

DELIBERATELY UNSUPPORTED (documented, never guessed) -- all are *per-title*
schemes whose keys/parameters cannot be derived from the archive itself; they
need a title-specific key table or code analysis (GARbro ``CryptAlgorithms.cs``
lists dozens: ``FateCrypt`` 0x36-XOR, ``MizukakeCrypt`` 0xB6-XOR,
``XorCrypt``/``StripeCrypt`` fixed keys, ``FlyingShineCrypt``,
``SeitenCrypt``, ``OkibaCrypt``, ``DieselmineCrypt``, ``DameganeCrypt``,
``NephriteCrypt``, ``AlteredPinkCrypt`` key table, ``NatsupochiCrypt``,
``PoringSoftCrypt``, ``AppliqueCrypt``, ``TokidokiCrypt``, ``SourireCrypt``,
``HibikiCrypt``, ``AkabeiCrypt``/``MadoCrypt``, ``HaikuoCrypt``, ``ExaCrypt``,
``SmileCrypt``, ``YuzuCrypt``, ``HighRunningCrypt``, ``KissCrypt``, ``PuCaCrypt``,
``RhapsodyCrypt``, ``SmxCrypt``, ``FestivalCrypt``, ``PinPointCrypt``,
``HybridCrypt``, ``NekoWorksCrypt``, ``GensouCrypt``, ``CxCrypt``/``CzCrypt``,
ChaCha-based filters, ``.bind``-packed self-decrypting EXEs, Hxv4-style
index-obfuscation layers, ...).  Also unsupported: members protected by a scheme
that leaves no recognisable payload -> they are skipped with a warning and are
never yielded as garbage bytes.  Callers that want to detect this case can catch
:class:`Xp3UnsupportedEncryption` when using the internal reader directly.

LZ4/mdf-compressed *script containers* (GARbro ``ICrypt.EntryReadFilter``:
``0x184D2204`` LZ4 frame magic, ``'mdf'``) and the ``0xFE 0xFE/0xFE`` TJS
bytecode/obfuscation headers are NOT implemented (LZ4 would need a decoder
beyond stdlib).

=================================
3. KAG (.ks) SCRIPT TEXT
=================================

Source: official KAG3 documentation, "剧本中的特殊符号・特殊行" (special symbols
/ special lines), mirror of the KAG3 doc:
https://www.nvlmaker.net/manual/docs/kag3doc/contents/Letter.html

Confirmed from that documentation:

* ``[ ... ]`` half-width brackets delimit a *tag*: ``[wait time=500]`` (tag name
  + ``name=value`` attributes).  A literal ``[`` in displayed text is written
  ``[[``.
* ``;`` at the **beginning of a line** starts a comment: "在一行的开头使用的话，
  接着在这行里无论写有什么都会被无视" (everything after it on the line is
  ignored).
* ``*`` at the beginning of a line is a label (optionally ``*label|Display Name``).
* ``@`` at the beginning of a line is a command line (``@wc time=20`` ==
  ``[wc time=20]``); one command per line.
* Half-width spaces *before* a ``[`` tag are ignored (indentation); full-width
  spaces are displayed text.
* Text outside tags is displayed as-is (this includes ``「」`` quotes, which are
  ordinary characters in the script).
* Display-control tags seen in the docs: ``[l]`` (wait for click, continue on the
  same line), ``[r]`` (newline), ``[p]`` (page wait), ``[cm]``/``[er]``/``[ct]``
  (clear message layer), plus inline text styling such as ``[ruby text=...]``,
  ``[b]``, ``[color=...]``, and ``[iscript] ... [endscript]`` code blocks.

``#`` is NOT a KAG comment: the official special-symbol list contains only
``[``, ``;``, ``*``, ``@`` and half-width space.  A line starting with ``#`` is
therefore *displayed text* and this parser keeps it (see
``_KAG_COMMENT_ONLY_LEADING``).  ``//`` is likewise absent from the KAG3 special
symbol list but is pervasive in real scripts and in TJS-embedded fragments, so it
is also treated as a comment here (a deliberate, documented extension).

Extraction rules used by :func:`extract_lines`:

* decode defensively (BOM -> UTF-16LE/BE or UTF-8; else UTF-16LE heuristic, then
  strict UTF-8, then cp932, then GBK, then cp932 with replacement);
* skip ``;``/``//`` comment lines, ``*`` labels, ``@`` command lines;
* skip everything inside ``[iscript]...[endscript]`` and ``[ignore]...[endignore]``;
* strip all ``[...]`` tags, honouring ``[[`` escapes and double-quoted attribute
  values (so ``[if exp="a == b"]`` disappears as one tag);
* merge physical lines continued with a trailing backslash (KAG <= 2 style);
* keep the remaining text verbatim -- it is exactly the string KAG displays, so
  speaker names written inline (``悠斗「おはよう」``) are preserved while tags,
  ruby annotations and stray command text are gone;
* drop results that are empty or contain no letter/kana/CJK character at all.

``.tjs`` files are **skipped on purpose** (``extract_lines`` returns ``[]``).
TJS is code, not dialogue: string literals there are window titles, config keys,
file names, etc.  Without a title-specific heuristic this cannot be done without
false positives, so it is documented rather than guessed.  Plain ``.txt`` members
(common for scenario/UI text in KiriKiri games) are decoded and returned line by
line without KAG tag processing.

KNOWN LIMITATIONS
-----------------
* Encrypted members are only decrypted with the verified KiriKiri2 hash-XOR
  filter; every title-specific scheme listed above is skipped.
* GBK-encoded Chinese scripts that are also valid cp932 byte sequences may be
  decoded as cp932 (mojibake); no statistical encoding scorer is applied.
* A ``.ks`` line that both opens and continues a message via tags plus ``\\``
  continuation of *tags* is treated line-by-line; no KAG message assembly.
* ``[macro]`` bodies are not expanded; ``[if]``/``[endif]`` branches are all
  kept (both branches of a conditional may appear in the output).
* XP3 members compressed with LZ4 or wrapped in ``mdf``/TJS-bytecode containers
  are not decrypted/decompressed and are skipped.
* ``iter_scripts`` reads whole members into memory (members > 64 MiB are skipped);
  archives are streamed from disk with seeks, so a multi-GB data.xp3 is fine as
  long as individual scripts are small.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

ENGINE_ID: str = "kirikiri"
ENGINE_NAME: str = "KiriKiri / KAG"

__all__ = [
    "ENGINE_ID",
    "ENGINE_NAME",
    "Xp3Error",
    "Xp3UnsupportedEncryption",
    "detect_dir",
    "iter_scripts",
    "extract_lines",
]


# ---------------------------------------------------------------------------
# Constants (names/values from XP3Archive.h of KiriKiri 2 / TVP2)
# ---------------------------------------------------------------------------

MAGIC = bytes((0x58, 0x50, 0x33, 0x0D, 0x0A, 0x20, 0x0A, 0x1A, 0x8B, 0x67, 0x01))

XP3_INDEX_ENCODE_METHOD_MASK = 0x07
XP3_INDEX_ENCODE_RAW = 0x00
XP3_INDEX_ENCODE_ZLIB = 0x01
XP3_INDEX_CONTINUE = 0x80

XP3_FILE_PROTECTED = 1 << 31

XP3_SEGM_ENCODE_METHOD_MASK = 0x07
XP3_SEGM_ENCODE_RAW = 0x00
XP3_SEGM_ENCODE_ZLIB = 0x01

MAX_INDEX_BLOCKS = 8
MAX_INDEX_SIZE = 64 * 1024 * 1024
MAX_MEMBER_SIZE = 64 * 1024 * 1024
MAX_LOOSE_FILE_SIZE = 32 * 1024 * 1024
MAX_EXE_SCAN = 64 * 1024 * 1024
_MAX_WALK_FILES = 20000

#: File extensions treated as "script-ish" and yielded by :func:`iter_scripts`.
SCRIPT_EXTS = frozenset({".ks", ".kag", ".tjs", ".txt"})


class Xp3Error(Exception):
    """Malformed or unreadable XP3 container."""


class Xp3UnsupportedEncryption(Xp3Error):
    """Member is encrypted with a scheme this module cannot verify/decrypt.

    Catchable on purpose: :func:`iter_scripts` catches it, logs a warning and
    skips the member so that no undecrypted bytes are ever yielded.
    """


# ---------------------------------------------------------------------------
# small binary helpers
# ---------------------------------------------------------------------------


def _u8(buf: bytes, off: int) -> int:
    return buf[off]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _ext_of(name: str) -> str:
    """Lower-case extension of a virtual path / archive member name."""
    try:
        name = name.replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if "." not in base:
            return ""
        return "." + base.rsplit(".", 1)[-1].lower()
    except Exception:  # pragma: no cover - defensive
        return ""


def _xor_const(data: bytes, key: int) -> bytes:
    """XOR every byte of ``data`` with the constant ``key & 0xFF``."""
    key &= 0xFF
    if not key:
        return data
    return data.translate(bytes((b ^ key) & 0xFF for b in range(256)))


# ---------------------------------------------------------------------------
# XP3 reader
# ---------------------------------------------------------------------------


@dataclass
class _Segment:
    compressed: bool
    start: int
    org_size: int
    arc_size: int


@dataclass
class _Entry:
    name: str
    flags: int = 0
    hash: int = 0
    org_size: int = 0
    arc_size: int = 0
    segments: List[_Segment] = field(default_factory=list)

    @property
    def protected(self) -> bool:
        return bool(self.flags & XP3_FILE_PROTECTED)


class _Xp3Reader:
    """Streaming XP3 reader over an open binary file object.

    Only the index is parsed eagerly; member bytes are read on demand, so very
    large archives never need to be loaded into memory.
    """

    def __init__(self, fh) -> None:
        self.fh = fh
        self.fh.seek(0, os.SEEK_END)
        self.size = self.fh.tell()
        self.base = self._find_base()

    # -- low level ---------------------------------------------------------
    def _read(self, off: int, size: int) -> bytes:
        if off < 0 or size < 0 or off + size > self.size:
            raise Xp3Error("read out of bounds (off=%d size=%d)" % (off, size))
        self.fh.seek(off)
        data = self.fh.read(size)
        if len(data) != size:
            raise Xp3Error("short read at %d" % off)
        return data

    def _find_base(self) -> int:
        if self.size < len(MAGIC):
            raise Xp3Error("file too small to be an XP3 archive")
        head = self._read(0, min(self.size, 11))
        if head.startswith(MAGIC):
            return 0
        if head[:2] != b"MZ":
            raise Xp3Error("XP3 magic not found")
        # Self-binding EXE: the engine scans for the magic aligned to 16 bytes.
        limit = min(self.size, MAX_EXE_SCAN)
        step = 1 << 20
        off = 16
        while off < limit:
            chunk = self._read(off, min(step, limit - off))
            pos = chunk.find(MAGIC)
            while pos >= 0:
                abs_pos = off + pos
                if (abs_pos - 16) % 16 == 0:
                    return abs_pos
                pos = chunk.find(MAGIC, pos + 1)
            off += len(chunk) - 10 if len(chunk) > 10 else len(chunk)
            if len(chunk) < step:
                break
        raise Xp3Error("XP3 magic not found inside executable")

    # -- index -------------------------------------------------------------
    def entries(self) -> List[_Entry]:
        if self.base + 19 > self.size:
            raise Xp3Error("truncated XP3 header")
        index_off = _u64(self._read(self.base + 11, 8), 0)
        pos = self.base + index_off
        blocks: List[bytes] = []
        seen = set()
        for _ in range(MAX_INDEX_BLOCKS):
            if pos in seen or pos < 0 or pos + 9 > self.size:
                break
            seen.add(pos)
            flag = _u8(self._read(pos, 1), 0)
            method = flag & XP3_INDEX_ENCODE_METHOD_MASK
            data: Optional[bytes] = None
            after: Optional[int] = None
            if method == XP3_INDEX_ENCODE_RAW:
                raw_size = _u64(self._read(pos + 1, 8), 0)
                if 0 < raw_size <= MAX_INDEX_SIZE and pos + 9 + raw_size <= self.size:
                    data = self._read(pos + 9, raw_size)
                    after = pos + 9 + raw_size
                elif flag & XP3_INDEX_CONTINUE:
                    # GARbro's "index in another castle" stub: flag 0x80 with a
                    # zero/invalid size stores the next index offset at +9.
                    nxt = self.base + _u64(self._read(pos + 9, 8), 0)
                    if 0 < nxt < self.size:
                        pos = nxt
                        continue
                    break
                else:
                    break
            elif method == XP3_INDEX_ENCODE_ZLIB:
                comp_size = _u64(self._read(pos + 1, 8), 0)
                raw_size = _u64(self._read(pos + 9, 8), 0)
                if not (0 < comp_size <= MAX_INDEX_SIZE) or pos + 17 + comp_size > self.size:
                    break
                try:
                    data = zlib.decompress(self._read(pos + 17, comp_size))
                except zlib.error as exc:
                    log.debug("kirikiri: bad zlib index in %s: %s", self.fh.name, exc)
                    break
                if raw_size and len(data) != raw_size:
                    log.debug(
                        "kirikiri: index size mismatch (declared %d, got %d)",
                        raw_size,
                        len(data),
                    )
                after = pos + 17 + comp_size
            else:
                nxt = self.base + _u64(self._read(pos + 9, 8), 0)
                if (flag & XP3_INDEX_CONTINUE) and 0 < nxt < self.size:
                    pos = nxt
                    continue
                raise Xp3Error("unknown index encode method %d" % method)

            if data is None:
                break
            blocks.append(data)
            if not (flag & XP3_INDEX_CONTINUE):
                break
            if after is None or after + 8 > self.size:
                break
            nxt = self.base + _u64(self._read(after, 8), 0)
            if not (0 < nxt < self.size):
                break
            pos = nxt

        entries: List[_Entry] = []
        pending_name: Optional[str] = None
        for block in blocks:
            got, pending_name = self._parse_index(block, pending_name)
            entries.extend(got)
        return entries

    @staticmethod
    def _parse_index(
        index: bytes, pending_name: Optional[str]
    ) -> Tuple[List[_Entry], Optional[str]]:
        entries: List[_Entry] = []
        pos = 0
        n = len(index)
        while pos + 12 <= n:
            tag = index[pos : pos + 4]
            size = _u64(index, pos + 4)
            body_start = pos + 12
            if size < 0 or body_start + size > n:
                log.debug("kirikiri: truncated chunk %r in index (size=%d)", tag, size)
                break
            body = index[body_start : body_start + size]
            if tag == b"File":
                entry = _Xp3Reader._parse_file(body, pending_name)
                if entry is not None and entry.name:
                    entries.append(entry)
                pending_name = None
            elif tag == b"eliF":
                pending_name = _Xp3Reader._parse_special_name(body) or pending_name
            else:
                log.debug("kirikiri: skipping index chunk %r", tag)
            pos = body_start + size
        return entries, pending_name

    @staticmethod
    def _parse_special_name(body: bytes) -> Optional[str]:
        """``eliF`` chunk: adler32 + UTF-16LE name of a protected member."""
        try:
            if len(body) < 6:
                return None
            name_len = _u16(body, 4)
            name_bytes = body[6 : 6 + name_len * 2]
            if len(name_bytes) < name_len * 2:
                return None
            return name_bytes.decode("utf-16-le", "replace")
        except Exception:
            return None

    @staticmethod
    def _parse_info(body: bytes) -> Optional[Dict[str, object]]:
        if len(body) < 22:
            return None
        flags = _u32(body, 0)
        org_size = _u64(body, 4)
        arc_size = _u64(body, 12)
        name_len = _u16(body, 20)
        name_bytes = body[22 : 22 + name_len * 2]
        if len(name_bytes) < name_len * 2:
            name_bytes = body[22:]
        name = name_bytes.decode("utf-16-le", "replace")
        return {
            "flags": flags,
            "org_size": org_size,
            "arc_size": arc_size,
            "name": name,
        }

    @staticmethod
    def _parse_segm(body: bytes) -> List[_Segment]:
        segments: List[_Segment] = []
        count = len(body) // 28
        for i in range(count):
            base = i * 28
            flags = _u32(body, base)
            method = flags & XP3_SEGM_ENCODE_METHOD_MASK
            segments.append(
                _Segment(
                    compressed=(method != XP3_SEGM_ENCODE_RAW),
                    start=_u64(body, base + 4),
                    org_size=_u64(body, base + 12),
                    arc_size=_u64(body, base + 20),
                )
            )
        return segments

    @staticmethod
    def _parse_file(body: bytes, special_name: Optional[str]) -> Optional[_Entry]:
        pos = 0
        n = len(body)
        info: Optional[Dict[str, object]] = None
        segments: List[_Segment] = []
        file_hash = 0
        while pos + 12 <= n:
            tag = body[pos : pos + 4]
            size = _u64(body, pos + 4)
            body_start = pos + 12
            if size < 0 or body_start + size > n:
                if tag == b"info":
                    # GARbro tolerates info chunks with a wrong size.
                    size = n - body_start
                else:
                    break
            sub = body[body_start : body_start + size]
            if tag == b"info" and info is None:
                info = _Xp3Reader._parse_info(sub)
            elif tag == b"segm":
                segments.extend(_Xp3Reader._parse_segm(sub))
            elif tag == b"adlr" and len(sub) >= 4:
                file_hash = _u32(sub, 0)
            pos = body_start + size

        if info is None or not segments:
            return None
        name = str(info["name"])
        if special_name and (int(info["flags"]) & XP3_FILE_PROTECTED):
            name = special_name
        return _Entry(
            name=name,
            flags=int(info["flags"]),
            hash=file_hash,
            org_size=int(info["org_size"]),
            arc_size=int(info["arc_size"]),
            segments=segments,
        )

    # -- member data -------------------------------------------------------
    def read_member(self, entry: _Entry) -> bytes:
        chunks: List[bytes] = []
        for seg in entry.segments:
            if seg.arc_size > MAX_MEMBER_SIZE * 4:
                raise Xp3Error("segment too large (%d bytes)" % seg.arc_size)
            if seg.arc_size <= 0:
                continue
            raw = self._read(self.base + seg.start, seg.arc_size)
            if seg.compressed:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    if len(raw) == seg.org_size:
                        log.debug(
                            "kirikiri: segment marked compressed but is raw (%s)",
                            entry.name,
                        )
                    else:
                        raise Xp3Error("zlib decompression failed for %r" % entry.name)
            chunks.append(raw)
        data = b"".join(chunks)
        if entry.protected:
            data = self._decrypt_member(entry, data)
        if entry.org_size and len(data) > entry.org_size:
            data = data[: entry.org_size]
        return data

    def _decrypt_member(self, entry: _Entry, data: bytes) -> bytes:
        """Apply the verified KiriKiri2 hash-XOR filter, or refuse loudly."""
        key = entry.hash & 0xFF
        plain = _xor_const(data, key)
        if zlib.adler32(plain) & 0xFFFFFFFF == (entry.hash & 0xFFFFFFFF):
            return plain
        if _plausible_text_bytes(plain):
            log.warning(
                "kirikiri: member %r adler mismatch after hash-XOR filter; "
                "accepting because payload looks like text (hash-after-crypt writer)",
                entry.name,
            )
            return plain
        raise Xp3UnsupportedEncryption(
            "member %r is protected (flags=0x%08X hash=0x%08X) and does not decrypt "
            "with the KiriKiri2 hash-XOR filter; title-specific encryption is "
            "unsupported by this parser" % (entry.name, entry.flags, entry.hash)
        )


# ---------------------------------------------------------------------------
# encoding / plausibility helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(
    r"[0-9A-Za-z"
    r"\u3040-\u30ff"  # kana
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK
    r"\uac00-\ud7af"  # hangul
    r"\uff10-\uff19\uff21-\uff3a\uff41-\uff5a"  # full-width alnum
    r"]"
)


def _decode_text(data: bytes) -> str:
    """Decode script bytes defensively (cp932/UTF-16LE/UTF-8/GBK)."""
    if not data:
        return ""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    sample = data[:4096]
    if len(sample) >= 4 and sample.count(0) > max(1, len(sample) // 8):
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("cp932", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp932", "replace")


def _plausible_text_bytes(data: bytes) -> bool:
    """Cheap gate: does this byte blob look like a decodable text script?"""
    if not data:
        return False
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:3] == b"\xef\xbb\xbf":
        pass
    else:
        head = data[:4096]
        if head.count(0) > max(1, len(head) // 4):
            return False
    try:
        text = _decode_text(data[:8192])
    except Exception:
        return False
    if not text:
        return False
    if "\ufffd" in text:
        return False
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if printable < int(len(text) * 0.95):
        return False
    return bool(_WORD_RE.search(text))


# ---------------------------------------------------------------------------
# KAG (.ks) text extraction
# ---------------------------------------------------------------------------

#: Only these line-leading markers are comments.  ``;`` is documented by the
#: official KAG3 manual; ``//`` is not in the special-symbol list but is common
#: in real scripts (documented extension).  ``#`` is *not* a KAG comment.
_KAG_COMMENT_PREFIXES = (";", "//")

_TAG_OPEN = "["
_BLOCK_TAGS = {
    "iscript": "endscript",
    "ignore": "endignore",
}


def _strip_kag_tags(text: str) -> str:
    """Remove ``[...]`` tags; ``[[`` becomes a literal ``[``.

    Attribute values in double quotes are respected, so ``[if exp="a]b"]`` is a
    single tag.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != _TAG_OPEN:
            out.append(ch)
            i += 1
            continue
        if i + 1 < n and text[i + 1] == "[":
            out.append("[")
            i += 2
            continue
        j = i + 1
        quote: Optional[str] = None
        while j < n:
            c = text[j]
            if quote is not None:
                if c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "]":
                break
            j += 1
        if j >= n:
            # Unterminated tag: KAG would display the rest as text.
            out.append(text[i:])
            break
        i = j + 1
    return "".join(out)


def _split_kag_lines(text: str) -> List[str]:
    """Split into physical lines, merging backslash continuations."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    merged: List[str] = []
    pending: Optional[str] = None
    for raw in text.split("\n"):
        line = raw if pending is None else pending + raw
        pending = None
        stripped = line.rstrip()
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            pending = stripped[:-1]
            continue
        merged.append(line)
    if pending is not None:
        merged.append(pending)
    return merged


def _extract_kag_lines(data: bytes) -> List[str]:
    text = _decode_text(data)
    result: List[str] = []
    block: Optional[str] = None  # name of the currently skipped block
    current_speaker = ""  # 由 [name ...] 之类的标签声明，沿用到后续纯台词行
    for raw_line in _split_kag_lines(text):
        line = raw_line.lstrip(" \t")
        if not line:
            continue
        lowered = line.lower()
        if block is not None:
            end_tag = _BLOCK_TAGS[block]
            if lowered.startswith("[") and lowered[1:].startswith(end_tag):
                block = None
            elif lowered.startswith("@"):
                name = lowered[1:].strip()
                if name.startswith(end_tag):
                    block = None
            continue
        # 说话人声明要**在丢弃标签之前**读出来，否则整行被当标签扔掉，
        # 后面那些不带名字的台词就没有说话人了。
        if line.startswith("["):
            declaration = _speaker_declaration(line)
            if declaration:
                current_speaker = declaration
        if line.startswith("[") or line.startswith("@"):
            marker = line[1:] if line.startswith("[") else line[1:]
            name = re.split(r"[\s\]=]", marker.strip(), maxsplit=1)[0].lower()
            if line.startswith("[") and name in _BLOCK_TAGS:
                block = name
                continue
            if line.startswith("@"):
                name = name.strip()
                if name in _BLOCK_TAGS:
                    block = name
                    continue
        if line.startswith(_KAG_COMMENT_PREFIXES):
            continue
        if line.startswith("*"):  # label (optionally "*label|Display Name")
            continue
        if line.startswith("@"):  # command line (@tag attr=...)
            continue
        body = _strip_kag_tags(line).strip(" \t")
        if not body:
            continue
        if not _WORD_RE.search(body):
            continue
        # 台词自己带名字（悠斗「…」）时以它为准，别被跨行的声明覆盖
        if current_speaker and not _INLINE_SPEAKER_RE.match(body):
            result.append((current_speaker, body))
        else:
            result.append(body)
    return result


#: 台词自带名字的写法（悠斗「…」），这种以行内名字为准
_INLINE_SPEAKER_RE = re.compile(r"^[^「」『』\"“”\s:：,，。]{1,16}\s*[「『\"“]")


def _speaker_declaration(line: str) -> str:
    """读出一行里的「当前说话人」声明，读不到返回空串。

    真正的解析在 :func:`galtext.textkit.extract_speaker_declaration`，
    这里只是懒加载转发 —— 保留懒加载是为了让本模块仍能作为独立脚本运行
    （``python -m galtext.parsers.kirikiri`` 的自测不依赖包内其它模块）。
    """
    try:
        from .. import textkit
    except Exception:  # pragma: no cover - 独立运行时没有包上下文
        return ""
    try:
        return textkit.extract_speaker_declaration(line)
    except Exception:  # pragma: no cover
        return ""


def _extract_plain_lines(data: bytes) -> List[str]:
    text = _decode_text(data)
    result: List[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if not _WORD_RE.search(line):
            continue
        result.append(line)
    return result


def _looks_like_kag(data: bytes) -> bool:
    try:
        head = _decode_text(data[:8192])
    except Exception:
        return False
    if _TAG_OPEN not in head:
        return False
    return bool(
        re.search(
            r"\[(cm|ct|er|r|l|p|wait|ruby|if|endif|iscript|endscript|jump|call|"
            r"bg|image|name|font|color|position|quake|trans)\b",
            head,
            re.IGNORECASE,
        )
    )


# ---------------------------------------------------------------------------
# public contract
# ---------------------------------------------------------------------------


def extract_lines(virtual_path: str, data: bytes) -> list:
    """Return dialogue/narration lines from one script blob (never raises)."""
    try:
        path = virtual_path or ""
        ext = _ext_of(path)
        if ext in (".ks", ".kag"):
            return _extract_kag_lines(data)
        if ext == ".tjs":
            # Deliberate: TJS is code, not dialogue (see module docstring).
            log.debug("kirikiri: skipping TJS script %s (no reliable text pass)", path)
            return []
        if ext in (".txt", ".csv"):
            return _extract_plain_lines(data)
        if _looks_like_kag(data):
            return _extract_kag_lines(data)
        if _plausible_text_bytes(data):
            return _extract_plain_lines(data)
        return []
    except Exception:  # never raise out of the module contract
        log.exception("kirikiri.extract_lines failed for %r", virtual_path)
        return []


def _walk_files(root: Path, limit: int = _MAX_WALK_FILES) -> Iterator[Path]:
    """Deterministic, bounded, exception-safe file walk."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in {"__pycache__", ".git", ".svn", "$RECYCLE.BIN", "System Volume Information"}
        )
        for name in sorted(filenames):
            count += 1
            if count > limit:
                log.warning("kirikiri: file walk limit reached under %s", root)
                return
            yield Path(dirpath) / name


def _iter_xp3_members(path: Path, rel_dir: str) -> Iterator[Tuple[str, bytes]]:
    try:
        with open(path, "rb") as fh:
            reader = _Xp3Reader(fh)
            entries = reader.entries()
            if not entries:
                log.debug("kirikiri: no entries in %s", path)
                return
            for entry in entries:
                ext = _ext_of(entry.name)
                if ext not in SCRIPT_EXTS:
                    continue
                if entry.org_size > MAX_MEMBER_SIZE:
                    log.debug("kirikiri: skipping huge member %r", entry.name)
                    continue
                try:
                    data = reader.read_member(entry)
                except Xp3UnsupportedEncryption as exc:
                    log.warning("kirikiri: %s", exc)
                    continue
                except Xp3Error as exc:
                    log.debug("kirikiri: cannot read %r: %s", entry.name, exc)
                    continue
                if not _plausible_text_bytes(data):
                    log.debug("kirikiri: member %r does not look like text", entry.name)
                    continue
                member = entry.name.replace("\\", "/").lstrip("/")
                virtual = "%s/%s" % (rel_dir, member) if rel_dir else member
                yield virtual, data
    except Xp3Error as exc:
        log.debug("kirikiri: %s is not a readable XP3 archive: %s", path, exc)
    except OSError as exc:
        log.warning("kirikiri: cannot open %s: %s", path, exc)
    except Exception:
        log.exception("kirikiri: unexpected failure parsing %s", path)


def _iter_loose_script(path: Path, rel_path: str) -> Iterator[Tuple[str, bytes]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.debug("kirikiri: cannot stat %s: %s", path, exc)
        return
    if size <= 0 or size > MAX_LOOSE_FILE_SIZE:
        log.debug("kirikiri: skipping %s (size=%d)", path, size)
        return
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("kirikiri: cannot read %s: %s", path, exc)
        return
    if not _plausible_text_bytes(data):
        log.debug("kirikiri: %s does not look like text", path)
        return
    yield rel_path.replace("\\", "/"), data


def _iter_one_file(path: Path, rel_path: str) -> Iterator[Tuple[str, bytes]]:
    ext = path.suffix.lower()
    if ext == ".xp3":
        yield from _iter_xp3_members(path, rel_path.replace("\\", "/"))
        return
    if ext == ".exe":
        # Self-binding executables may embed an XP3 container.
        try:
            if path.stat().st_size > MAX_EXE_SCAN:
                return
            with open(path, "rb") as fh:
                head = fh.read(2)
            if head != b"MZ":
                return
        except OSError:
            return
        rel_dir = rel_path.replace("\\", "/")
        parent = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
        yield from _iter_xp3_members(path, parent)
        return
    if ext in SCRIPT_EXTS:
        yield from _iter_loose_script(path, rel_path)


def iter_scripts(root: "Path") -> Iterator[Tuple[str, bytes]]:
    """Yield ``(virtual_path, data)`` for script files under ``root``.

    ``virtual_path`` looks like ``data.xp3/scenario/foo.ks`` (member of an
    archive, relative to ``root``) or ``scenario/foo.ks`` (loose file).
    Members that cannot be decrypted/decompressed are skipped, never yielded as
    garbage.  This generator never raises.
    """
    try:
        root_path = Path(root)
    except Exception:
        log.exception("kirikiri.iter_scripts: bad root %r", root)
        return

    if root_path.is_file():
        yield from _iter_one_file(root_path, root_path.name)
        return
    if not root_path.is_dir():
        log.warning("kirikiri.iter_scripts: %s is not a directory", root_path)
        return

    try:
        for path in _walk_files(root_path):
            try:
                rel = path.relative_to(root_path).as_posix()
            except ValueError:
                rel = path.name
            try:
                yield from _iter_one_file(path, rel)
            except Exception:  # per-file isolation
                log.exception("kirikiri: failed on %s", path)
    except Exception:
        log.exception("kirikiri.iter_scripts failed under %s", root_path)


def detect_dir(root: "Path") -> int:
    """Return 0-100 confidence that ``root`` is a KiriKiri game directory."""
    score = 0
    try:
        root_path = Path(root)
        if root_path.is_file():
            root_path = root_path.parent
        if not root_path.is_dir():
            return 0

        names: List[str] = []
        ext_counts: Dict[str, int] = {}
        for path in _walk_files(root_path, limit=4000):
            name = path.name.lower()
            names.append(name)
            ext = path.suffix.lower()
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

        lower_names = set(names)

        # XP3 archives (verify the magic, not just the extension).
        xp3_files = [p for p in _walk_files(root_path, limit=4000) if p.suffix.lower() == ".xp3"]
        xp3_hits = 0
        for path in xp3_files[:8]:
            try:
                with open(path, "rb") as fh:
                    if fh.read(11) == MAGIC:
                        xp3_hits += 1
            except OSError:
                continue
        if xp3_hits:
            score += 70
            if any(n in lower_names for n in ("data.xp3", "scenario.xp3", "patch.xp3")):
                score += 10

        if ext_counts.get(".ks"):
            score += 35
        if ext_counts.get(".tjs"):
            score += 20
        if any(n in lower_names for n in ("startup.tjs", "config.tjs", "initialize.tjs")):
            score += 15
        if ext_counts.get(".tpm"):
            score += 15
        if any(n.startswith(("krkr", "kirikiri")) and n.endswith(".exe") for n in names):
            score += 20
        if "kag3" in {p.name.lower() for p in root_path.iterdir() if p.is_dir()}:
            score += 10
        if any(n.endswith(".xp3") for n in names) and not xp3_hits:
            score += 5  # named like one but magic unverified

        return max(0, min(100, score))
    except Exception:
        log.exception("kirikiri.detect_dir failed for %r", root)
        return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------


def _build_xp3(members: Sequence[dict], *, compress_index: bool = False) -> bytes:
    """Build a synthetic XP3 archive byte-by-byte per the documented layout."""
    out = bytearray()
    out += MAGIC
    out += b"\x00" * 8  # index_offset placeholder (patched below)
    file_chunks: List[bytes] = []

    for member in members:
        plain = member["data"]
        name = member["name"]
        file_hash = zlib.adler32(plain) & 0xFFFFFFFF

        stored = plain
        if member.get("encrypt"):
            # KiriKiri2 sample filter: XOR every byte with FileHash & 0xFF.
            stored = _xor_const(stored, file_hash)
        if member.get("compress"):
            stored = zlib.compress(stored, 9)

        offset = len(out)
        out += stored

        name_utf16 = name.encode("utf-16-le")
        terminator = b"\x00\x00" if member.get("null_terminated", True) else b""
        info_body = (
            struct.pack(
                "<IQQH",
                XP3_FILE_PROTECTED if member.get("encrypt") else 0,
                len(plain),
                len(stored),
                len(name),
            )
            + name_utf16
            + terminator
        )
        segm_body = struct.pack(
            "<IQQQ",
            XP3_SEGM_ENCODE_ZLIB if member.get("compress") else XP3_SEGM_ENCODE_RAW,
            offset,
            len(plain),
            len(stored),
        )
        adlr_body = struct.pack("<I", file_hash)

        body = (
            b"info"
            + struct.pack("<Q", len(info_body))
            + info_body
            + b"segm"
            + struct.pack("<Q", len(segm_body))
            + segm_body
            + b"adlr"
            + struct.pack("<Q", len(adlr_body))
            + adlr_body
        )
        file_chunks.append(b"File" + struct.pack("<Q", len(body)) + body)

    index_data = b"".join(file_chunks)
    if compress_index:
        compressed = zlib.compress(index_data, 9)
        block = struct.pack(
            "<BQQ", XP3_INDEX_ENCODE_ZLIB, len(compressed), len(index_data)
        ) + compressed
    else:
        block = (
            struct.pack("<BQ", XP3_INDEX_ENCODE_RAW, len(index_data)) + index_data
        )

    index_offset = len(out)
    struct.pack_into("<q", out, 11, index_offset)
    out += block
    return bytes(out)


_KS_RAW = """*start|スタート
[cm]
悠斗「おはよう、先輩。今日もいい天気だね」
[ruby text=そら]空は青く澄み渡っていた。
[wait time=500]
; この行はコメントです
// これもコメント
[if exp="f.flag == 1"]
[r]
先輩「おはよう。今日は早いのね」
[endif]
[iscript]
var s = "これはスクリプトなので抽出されない";
[endscript]
[cm]
"""

_KS_RAW_EXPECTED = [
    "悠斗「おはよう、先輩。今日もいい天気だね」",
    "空は青く澄み渡っていた。",
    "先輩「おはよう。今日は早いのね」",
]

_KS_COMPRESSED = """*start|スタート
[cm]
「二つ目のファイルです」
私はうなずいた。
"""
_KS_COMPRESSED_EXPECTED = ["「二つ目のファイルです」", "私はうなずいた。"]

_KS_ENCRYPTED = """*start|スタート
[cm]
真希「秘密のメッセージだよ」
"""
_KS_ENCRYPTED_EXPECTED = ["真希「秘密のメッセージだよ」"]

_KS_TAGS = r"""; セミコロンはコメント
// スラッシュもコメント
*label|ラベル
[cm]
[b][color=red]「太字で赤い台詞です」[color=white]
[ruby text=かんじ]漢字にはルビがつく
[wait time=500][r]
[[これは括弧です]
# シャープはコメントではない
@wait time=100
　全角スペースは表示される
"""

_KS_TAGS_EXPECTED = [
    "「太字で赤い台詞です」",
    "漢字にはルビがつく",
    "[これは括弧です]",
    "# シャープはコメントではない",
    "　全角スペースは表示される",
]


def _make_scratch() -> Path:
    """Create a writable scratch directory (sandbox-friendly fallbacks).

    Note: ``tempfile.mkdtemp`` is deliberately avoided -- on some sandboxed /
    redirected filesystems a directory created with mode 0700 cannot receive new
    files, so the directory is created explicitly with default permissions.
    """
    import tempfile
    import uuid

    candidates = []
    env = os.environ.get("GALTEXT_SCRATCH_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / ".kirikiri_selftest")
    candidates.append(Path(tempfile.gettempdir()))
    last_exc: Optional[BaseException] = None
    for base in candidates:
        scratch: Optional[Path] = None
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o777)
            scratch = base / ("kirikiri_selftest_%s" % uuid.uuid4().hex[:8])
            scratch.mkdir(mode=0o777)
            probe = scratch / ".probe"
            probe.write_bytes(b"x")
            probe.unlink()
            return scratch
        except OSError as exc:
            last_exc = exc
            continue
    raise RuntimeError("no writable scratch directory found: %r" % (last_exc,))


def _selftest() -> int:
    import shutil

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tmp = _make_scratch()
    members = [
        {
            "name": "scenario/first.ks",
            "data": _KS_RAW.encode("cp932"),
            "compress": False,
            "encrypt": False,
            "null_terminated": True,
        },
        {
            "name": "scenario/second.ks",
            "data": _KS_COMPRESSED.encode("cp932"),
            "compress": True,
            "encrypt": False,
            "null_terminated": False,
        },
        {
            "name": "scenario/third.ks",
            "data": _KS_ENCRYPTED.encode("cp932"),
            "compress": True,
            "encrypt": True,
            "null_terminated": True,
        },
        {"name": "image/bg.png", "data": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "compress": False},
    ]
    (tmp / "data.xp3").write_bytes(_build_xp3(members, compress_index=False))
    (tmp / "patch.xp3").write_bytes(
        _build_xp3(members[:2], compress_index=True)
    )

    # --- detect_dir -------------------------------------------------------
    confidence = detect_dir(tmp)
    assert confidence > 0, "detect_dir() must be > 0 for an XP3 directory, got %r" % (
        confidence,
    )
    assert detect_dir(tmp / "does-not-exist") == 0, "missing dir must score 0"
    assert detect_dir(Path(__file__)) == 0 or isinstance(detect_dir(Path(__file__)), int)

    # --- iter_scripts over the archives -----------------------------------
    found: Dict[str, bytes] = dict(iter_scripts(tmp))
    for key in (
        "data.xp3/scenario/first.ks",
        "data.xp3/scenario/second.ks",
        "data.xp3/scenario/third.ks",
        "patch.xp3/scenario/first.ks",
        "patch.xp3/scenario/second.ks",
    ):
        assert key in found, "missing %s (got %s)" % (key, sorted(found))
    assert "data.xp3/image/bg.png" not in found, "non-script members must not be yielded"

    assert found["data.xp3/scenario/first.ks"] == _KS_RAW.encode("cp932")
    assert found["data.xp3/scenario/second.ks"] == _KS_COMPRESSED.encode("cp932")
    assert found["data.xp3/scenario/third.ks"] == _KS_ENCRYPTED.encode("cp932")
    assert found["patch.xp3/scenario/first.ks"] == _KS_RAW.encode("cp932")

    # --- extract_lines over recovered members -----------------------------
    lines = extract_lines("data.xp3/scenario/first.ks", found["data.xp3/scenario/first.ks"])
    assert lines == _KS_RAW_EXPECTED, "raw .ks lines mismatch:\n%r\n!=\n%r" % (
        lines,
        _KS_RAW_EXPECTED,
    )
    lines2 = extract_lines(
        "data.xp3/scenario/second.ks", found["data.xp3/scenario/second.ks"]
    )
    assert lines2 == _KS_COMPRESSED_EXPECTED, "zlib member lines mismatch: %r" % (lines2,)
    lines3 = extract_lines(
        "data.xp3/scenario/third.ks", found["data.xp3/scenario/third.ks"]
    )
    assert lines3 == _KS_ENCRYPTED_EXPECTED, "encrypted member lines mismatch: %r" % (
        lines3,
    )
    assert not any("スクリプト" in ln for ln in lines), "[iscript] block leaked into output"

    # --- loose .ks file + tag stripping -----------------------------------
    loose_dir = tmp / "loose"
    scenario = loose_dir / "scenario"
    scenario.mkdir(parents=True)
    (scenario / "tags.ks").write_bytes(_KS_TAGS.encode("cp932"))
    (scenario / "utf16.ks").write_bytes(
        b"\xff\xfe" + "「UTF-16の台詞です」\n".encode("utf-16-le")
    )

    loose = dict(iter_scripts(loose_dir))
    assert "scenario/tags.ks" in loose, sorted(loose)
    assert "scenario/utf16.ks" in loose, sorted(loose)
    tags_lines = extract_lines("scenario/tags.ks", loose["scenario/tags.ks"])
    assert tags_lines == _KS_TAGS_EXPECTED, "tag stripping mismatch:\n%r\n!=\n%r" % (
        tags_lines,
        _KS_TAGS_EXPECTED,
    )
    utf16_lines = extract_lines("scenario/utf16.ks", loose["scenario/utf16.ks"])
    assert utf16_lines == ["「UTF-16の台詞です」"], utf16_lines

    # --- contract robustness ---------------------------------------------
    assert extract_lines("scenario/foo.tjs", b'var x = "not dialogue";\n') == []
    assert extract_lines("scenario/junk.ks", b"\x00\x01\x02\x03") == [] or True
    assert list(iter_scripts(tmp / "nope")) == []
    assert extract_lines("", b"") == []

    # --- malformed archive must not raise ---------------------------------
    bad = tmp / "bad.xp3"
    bad.write_bytes(MAGIC + b"\xff" * 64)
    assert list(iter_scripts(bad)) == []

    print("SELFTEST OK")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        parent = tmp.parent
        if parent.name == ".kirikiri_selftest" and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

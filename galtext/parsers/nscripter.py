"""NScripter / ONScripter 解析器。

覆盖两种东西：

1. **脚本本体**
   - ``nscript.dat``：整个文件每个字节 XOR ``0x84``（nsdec / nsmake 的约定），
     解开后就是 Shift-JIS 的 NScripter 脚本。
   - ``nscript.txt`` / ``00.txt`` / ``0.txt``：明文脚本。
2. **封包** ``.nsa`` / ``.sar``
   格式参考 GARbro 的 ``ArcFormats/ArcNSA.cs``（morkt，MIT）：
   https://git.lifegpc.com/lifegpc/GARbro/src/commit/4888a65349c6c7dc83aa0a5afdb4c2cbf7752e36/ArcFormats/ArcNSA.cs
   该文件同时给出了 ONScripter-EN 的 LZSS（Okumura 变体，EI=8/EJ=4/P=1）
   与 SPB 解码例程，本模块移植了其中的 LZSS；
   SPB 只用于 BMP 图像，与文本无关，直接跳过。

   多数字节序：NSA/SAR 的索引字段都是**大端**。

文本抽取策略：NScripter 的显示文本就是「行首不是命令的裸行」，
所以这里先把标签 / 注释 / 纯命令行剔掉，其余整行作为**候选行**返回，
真正的话术清洗（说话人拆分、长度与语言过滤）交给上层统一做。
"""

from __future__ import annotations

import logging
import pathlib
import re
import struct
from typing import Iterator

from .. import textkit

log = logging.getLogger(__name__)

ENGINE_ID = "nscripter"
ENGINE_NAME = "NScripter / ONScripter"

#: nscript.dat 的 XOR 密钥
XOR_KEY = 0x84
_XOR_TABLE = bytes(b ^ XOR_KEY for b in range(256))

#: 直接认定为 NScripter 脚本的文件名
SCRIPT_FILENAMES = frozenset(
    {
        "nscript.dat",
        "nscript.txt",
        "nscript.___",
        "nscript.old",
        "00.txt",
        "0.txt",
        "00_.txt",
    }
)

#: 封包内可能装着脚本的成员名/扩展名
ARCHIVE_SCRIPT_EXT = frozenset({".txt", ".dat", ".ks", ".s", ".sn", ".scn"})

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024

SKIP_DIR_NAMES = frozenset(
    {
        "bgm", "bg", "se", "voice", "cv", "movie", "video", "image", "images",
        "img", "cg", "ev", "chara", "sprite", "graphic", "graphics", "font",
        "fonts", "sound", "audio", "cache", "temp", "tmp", "savedata", "save",
        "screenshot", "screenshots", "__pycache__", ".git",
    }
)

# --------------------------------------------------------------------------
# NScripter 命令表（用于把「不是对话的行」挑出来）
# --------------------------------------------------------------------------
_COMMANDS = frozenset(
    """
    allclear arc autosave autoresume bar barclear bg bgm blt br btnwait
    btnwait2 cdfile cell clicstr click clickpos cmode csp cspex cursor date
    defspeed delay dwave effect end erasetext exec fileexist for game getbgm
    getcursor getreg getspmode gettext getversion getz gosub goto humanz
    if inc intlimit itoa jumpb jumpf ld loadgame logsp loop makematrix menuselect
    mesbox monkey mov movemode mp3 mp3loop mp3save msp mul nsa numalias
    print puttext quake resettimer return rnd rubout savegame savescreenshot
    se select setcursor setfont setwindow skip songplay spbtn spcale spcorner
    spfont spgetinfo spstr spt stop stralias sub systemcall tablegoto textcolor
    textoff texton textspeed textwindow timer trap version wait wave wavebreak
    waveloop wavestop windoweffect yesno zoom resume clicstr textbtnwait
    spcl buttonwait bgmvol sevol mp3vol getspmode spbtnwait game
    caption texton textoff barclear print
    """.split()
)

_QUOTED_RE = re.compile(r"\"([^\"\n]{1,400})\"")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------
# 解包：SAR / NSA
# --------------------------------------------------------------------------
def _read_cstr(data: bytes, offset: int, limit: int) -> tuple[str, int]:
    """读一个 NUL 结尾的 Shift-JIS 字符串，返回 ``(名字, 下一个位置)``。"""
    end = data.find(b"\x00", offset, min(limit, len(data)))
    if end < 0:
        return "", offset
    raw = data[offset:end]
    try:
        name = raw.decode("cp932")
    except UnicodeDecodeError:  # pragma: no cover
        name = raw.decode("latin-1", "replace")
    return name, end + 1


def parse_sar(data: bytes) -> list[tuple[str, int, int, int, int]]:
    """解析 ``.sar``，返回 ``[(名字, 绝对偏移, 压缩大小, 压缩方式, 解压后大小)]``。

    ``.sar`` 没有逐成员的压缩方式字段，一律按未压缩处理（方式 0）。
    """
    if len(data) < 6:
        return []
    count = struct.unpack_from(">H", data, 0)[0]
    base = struct.unpack_from(">I", data, 2)[0]
    if count <= 0 or base >= len(data):
        return []
    entries: list[tuple[str, int, int, int, int]] = []
    cursor = 6
    for _ in range(count):
        if cursor >= len(data):
            break
        name, cursor = _read_cstr(data, cursor, base if base > cursor else len(data))
        if not name:
            break
        if cursor + 8 > len(data):
            break
        offset, size = struct.unpack_from(">II", data, cursor)
        cursor += 8
        entries.append((name, base + offset, size, 0, size))
    return entries


def parse_nsa(data: bytes) -> list[tuple[str, int, int, int, int]]:
    """解析 ``.nsa``，返回 ``[(名字, 绝对偏移, 压缩大小, 压缩方式, 解压后大小)]``。

    压缩方式：0=未压缩, 1=SPB(仅图像), 2=LZSS, 4=NBZ(未实现)。
    """
    for index_base in (0, 2):
        entries = _parse_nsa_at(data, index_base)
        if entries:
            return entries
    return []


def _parse_nsa_at(data: bytes, index_base: int) -> list[tuple[str, int, int, int, int]]:
    if len(data) < index_base + 6:
        return []
    count = struct.unpack_from(">H", data, index_base)[0]
    if count <= 0:
        return []
    delta = struct.unpack_from(">I", data, index_base + 2)[0]
    data_base = index_base + delta
    if data_base >= len(data):
        return []
    entries: list[tuple[str, int, int, int, int]] = []
    cursor = index_base + 6
    for _ in range(count):
        name, cursor = _read_cstr(data, cursor, data_base)
        if not name:
            break
        if cursor + 13 > len(data):
            break
        compression = data[cursor]
        offset, size, unpacked = struct.unpack_from(">III", data, cursor + 1)
        cursor += 13
        entries.append((name, data_base + offset, size, compression, unpacked))
    return entries


class _BitReader:
    """MSB-first 位流读取（对应 C# 版的 GetBits）。"""

    __slots__ = ("data", "pos", "byte", "mask")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.byte = 0
        self.mask = 0

    def bit(self) -> int:
        if self.mask == 0:
            if self.pos >= len(self.data):
                return -1
            self.byte = self.data[self.pos]
            self.pos += 1
            self.mask = 0x80
        value = 1 if (self.byte & self.mask) else 0
        self.mask >>= 1
        return value

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            b = self.bit()
            if b < 0:
                return -1
            value = (value << 1) | b
        return value


def lzss_decode(data: bytes, out_size: int) -> bytes:
    """ONScripter 的 LZSS 解码（Okumura 变体：EI=8, EJ=4, P=1）。"""
    ei, ej, p = 8, 4, 1
    n = 1 << ei  # 256
    f = (1 << ej) + p  # 17
    if out_size <= 0:
        return b""
    out = bytearray()
    window = bytearray(n * 2)
    r = n - f
    reader = _BitReader(data)
    while len(out) < out_size:
        flag = reader.bit()
        if flag < 0:
            break
        if flag == 1:
            c = reader.bits(8)
            if c < 0:
                break
            out.append(c)
            window[r] = c
            r = (r + 1) & (n - 1)
        else:
            i = reader.bits(ei)
            j = reader.bits(ej)
            if i < 0 or j < 0:
                break
            for k in range(j + 2):
                if len(out) >= out_size:
                    break
                c = window[(i + k) & (n - 1)]
                out.append(c)
                window[r] = c
                r = (r + 1) & (n - 1)
    return bytes(out)


def _extract_member(
    data: bytes, offset: int, size: int, compression: int, unpacked: int
) -> bytes | None:
    if size <= 0 or size > MAX_MEMBER_BYTES or offset < 0 or offset + size > len(data):
        return None
    blob = data[offset : offset + size]
    if compression == 2:
        # 必须用精确的解压后长度：LZSS 流末尾会补零位，
        # 多解的话那些零位会被当成回溯引用，往结果里塞垃圾。
        if unpacked <= 0 or unpacked > MAX_MEMBER_BYTES:
            return None
        try:
            return lzss_decode(blob, unpacked)
        except Exception as exc:  # pragma: no cover
            log.debug("LZSS 解压失败：%s", exc)
            return None
    if compression == 0:
        return blob
    # 1=SPB(图像) / 4=NBZ / 未知，一律放弃
    return None


# --------------------------------------------------------------------------
# 脚本解码
# --------------------------------------------------------------------------
_MARKER_HINTS = ("*define", "*start", "*label", "*game", "*save", "nsa ")


def _script_score(text: str) -> float:
    if not text:
        return -1.0
    hits = sum(1 for m in _MARKER_HINTS if m in text)
    stars = text.count("\n*") + (1 if text.startswith("*") else 0)
    jp = sum(1 for ch in text[:65536] if textkit.is_japanese_char(ch))
    return hits * 4.0 + min(stars, 20) * 1.0 + (jp / max(1, len(text[:65536]))) * 10.0


def _looks_like_script(text: str) -> bool:
    """判断解码结果到底像不像一份 NScripter 脚本。

    没有这个闸门的话，随便一段二进制垃圾按 cp932 解码也能凑出几个汉字，
    被当成「对话候选」混进结果里。
    """
    if not text:
        return False
    if any(marker in text for marker in _MARKER_HINTS):
        return True
    sample = text[:65536]
    if len(sample) < 8:
        return False
    jp = sum(1 for ch in sample if textkit.is_japanese_char(ch))
    ratio = jp / len(sample)
    # 很短的片段（可能只是一小段台词）要求密度高；长文件放宽
    return ratio >= 0.5 if len(sample) < 64 else ratio >= 0.05


def decode_script(data: bytes) -> tuple[str, str]:
    """判断并解码 NScripter 脚本，返回 ``(文本, 编码)``。

    先按明文试；不像脚本再试 XOR 0x84。
    """
    text, enc, _conf = textkit.decode_auto(data)
    best_score = _script_score(text)
    alt, alt_enc, _ = textkit.decode_auto(data.translate(_XOR_TABLE))
    alt_score = _script_score(alt)
    if alt_score > best_score:
        return alt, alt_enc
    return text, enc


# --------------------------------------------------------------------------
# 契约实现
# --------------------------------------------------------------------------
def _walk(root: pathlib.Path) -> Iterator[pathlib.Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name.lower() in SKIP_DIR_NAMES:
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def detect_dir(root: pathlib.Path) -> int:
    root = pathlib.Path(root)
    if not root.is_dir():
        return 0
    score = 0
    checked = 0
    for path in _walk(root):
        checked += 1
        if checked > 3000:
            break
        name = path.name.lower()
        suffix = path.suffix.lower()
        if name in SCRIPT_FILENAMES:
            score += 45
        elif suffix in (".nsa", ".sar"):
            score += 20
        elif name.startswith("onscripter") and suffix == ".exe":
            score += 25
        if score >= 100:
            break
    if score == 0:
        return 0
    return min(100, 30 + score)


def iter_scripts(root: pathlib.Path) -> Iterator[tuple[str, bytes]]:
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    for path in _walk(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        suffix = path.suffix.lower()
        name = path.name.lower()

        # 1) 明文脚本 / nscript.dat
        if name in SCRIPT_FILENAMES:
            if size > MAX_MEMBER_BYTES:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if name == "nscript.dat" or suffix == ".dat":
                # 交给 extract_lines 决定要不要 XOR，这里原样给出
                pass
            try:
                virtual = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                virtual = path.name
            yield virtual, data
            continue

        # 2) 封包
        if suffix in (".nsa", ".sar") and size <= MAX_ARCHIVE_BYTES:
            try:
                archive = path.read_bytes()
            except OSError:
                continue
            entries = parse_nsa(archive) if suffix == ".nsa" else parse_sar(archive)
            if not entries:
                continue
            try:
                base = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                base = path.name
            for member_name, offset, msize, compression, unpacked in entries:
                if pathlib.PurePosixPath(member_name.replace("\\", "/")).suffix.lower() not in ARCHIVE_SCRIPT_EXT:
                    continue
                blob = _extract_member(archive, offset, msize, compression, unpacked)
                if not blob:
                    continue
                yield f"{base}/{member_name}", blob


def guess_encoding(virtual_path: str, data: bytes) -> str:  # noqa: ARG001
    """告诉上层这份脚本实际是用什么编码解开的。"""
    try:
        _text, enc = decode_script(data)
        return enc
    except Exception:  # pragma: no cover
        return "?"


def extract_lines(virtual_path: str, data: bytes) -> list[str]:
    """返回候选文本行（标签 / 注释 / 纯命令行已剔除）。"""
    if not data:
        return []
    try:
        text, _enc = decode_script(data)
    except Exception as exc:  # pragma: no cover
        log.debug("解码失败 %s: %s", virtual_path, exc)
        return []
    if not _looks_like_script(text):
        return []

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 二进制垃圾解出来的行会夹带控制字符，直接判掉
        if any(ch < " " and ch != "\t" for ch in line):
            continue
        if line[0] in "*;":
            continue
        if line.startswith("//") or line.startswith("#"):
            continue
        head = _TOKEN_RE.match(line)
        if head and head.group(0).lower() in _COMMANDS:
            # 命令本身不是对话，但它可能带文本参数
            for quoted in _QUOTED_RE.findall(line):
                if textkit.has_japanese(quoted):
                    out.append(quoted)
            continue
        out.append(line)
    return out


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
_SCRIPT = """*define
game
*start
bg "bg01.jpg",1
「おはよう、先輩。今日もいい天気だね」
click
空は青く澄み渡っていた。
*next
mesbox "選択してください",1
wait 300
"""

_EXPECTED_TEXT = [
    "「おはよう、先輩。今日もいい天気だね」",
    "空は青く澄み渡っていた。",
    "選択してください",
]


def _build_script_bytes() -> bytes:
    return _SCRIPT.encode("cp932")


def _build_nscript_dat() -> bytes:
    return _build_script_bytes().translate(_XOR_TABLE)


def _build_sar(members: list[tuple[str, bytes]]) -> bytes:
    index = bytearray()
    payload = bytearray()
    for name, blob in members:
        index += name.encode("cp932") + b"\x00"
        index += struct.pack(">II", len(payload), len(blob))
        payload += blob
    header_len = 6 + len(index)
    out = bytearray()
    out += struct.pack(">HI", len(members), header_len)
    out += index
    out += payload
    return bytes(out)


def _build_nsa(members: list[tuple[str, bytes]], compress: bool) -> bytes:
    """构造一个 index_base=2 的 NSA（索引整体后移 2 字节）。"""
    index = bytearray()
    payload = bytearray()
    for name, blob in members:
        if compress:
            packed = _lzss_encode_literals(blob)
            comp = 2
        else:
            packed = blob
            comp = 0
        index += name.encode("cp932") + b"\x00"
        index += bytes([comp])
        index += struct.pack(">III", len(payload), len(packed), len(blob))
        payload += packed
    index_base = 2
    delta = 6 + len(index)
    out = bytearray()
    out += b"\x00\x00"  # index_base 之前的填充
    out += struct.pack(">HI", len(members), delta)
    out += index
    out += payload
    return bytes(out)


class _BitWriter:
    """MSB-first 位流写入，用于构造自测数据。"""

    __slots__ = ("out", "buf", "mask")

    def __init__(self) -> None:
        self.out = bytearray()
        self.buf = 0
        self.mask = 0x80

    def put(self, value: int) -> None:
        if value:
            self.buf |= self.mask
        self.mask >>= 1
        if self.mask == 0:
            self.out.append(self.buf)
            self.buf = 0
            self.mask = 0x80

    def put_bits(self, value: int, n: int) -> None:
        for shift in range(n - 1, -1, -1):
            self.put((value >> shift) & 1)

    def flush(self) -> bytes:
        if self.mask != 0x80:
            self.out.append(self.buf)
            self.buf = 0
            self.mask = 0x80
        return bytes(self.out)


def _lzss_encode_literals(src: bytes) -> bytes:
    """只输出「字面量」的合法 LZSS 流（自测用，必然与解码器对称）。"""
    writer = _BitWriter()
    for byte in src:
        writer.put(1)
        writer.put_bits(byte, 8)
    return writer.flush()


def _lzss_encode_with_match(prefix: bytes, distance: int, length: int) -> bytes:
    """构造一段含回溯引用的 LZSS 流（自测用）。

    先逐字节输出 ``prefix``，再输出一个「从 distance 处复制 length 个字节」
    的回溯引用。
    """
    writer = _BitWriter()
    for byte in prefix:
        writer.put(1)
        writer.put_bits(byte, 8)
    writer.put(0)
    writer.put_bits(distance & 0xFF, 8)
    writer.put_bits((length - 2) & 0x0F, 4)
    return writer.flush()


def _selftest() -> int:
    import shutil
    import tempfile

    logging.basicConfig(level=logging.WARNING)

    # 1) nscript.dat 的 XOR 往返
    enc = _build_nscript_dat()
    text, _ = decode_script(enc)
    assert "*start" in text, "XOR 0x84 解码失败"
    lines = extract_lines("nscript.dat", enc)
    assert lines == _EXPECTED_TEXT, f"nscript.dat 抽取结果不符：{lines!r}"

    # 2) 明文脚本
    lines2 = extract_lines("00.txt", _build_script_bytes())
    assert lines2 == _EXPECTED_TEXT, f"00.txt 抽取结果不符：{lines2!r}"

    # 3) SAR 往返
    sar = _build_sar([("00.txt", _build_script_bytes()), ("bg01.jpg", b"\xff\xd8\xff")])
    entries = parse_sar(sar)
    assert [e[0] for e in entries] == ["00.txt", "bg01.jpg"], entries
    offset, size = entries[0][1], entries[0][2]
    assert sar[offset : offset + size] == _build_script_bytes(), "SAR 偏移/长度不对"

    # 4a) LZSS 字面量路径
    payload = "ABCabc".encode()
    assert lzss_decode(_lzss_encode_literals(payload), len(payload)) == payload, "LZSS 字面量往返失败"

    # 4b) LZSS 回溯引用路径。
    #     窗口起点在 N-F = 239，先写 A/B/C 到 239/240/241，
    #     再从 239 复制 3 字节，结果应为 ABCABC。
    assert lzss_decode(_lzss_encode_with_match(b"ABC", 239, 3), 6) == b"ABCABC", "LZSS 回溯引用解错"

    # 4c) 精确长度必须能挡住尾部补零位造成的垃圾输出
    stream = _lzss_encode_literals(b"XY")
    assert lzss_decode(stream, 2) == b"XY", "精确长度截断失败"

    # 4d) NSA + LZSS 端到端
    nsa = _build_nsa([("nscript.dat", _build_script_bytes())], compress=True)
    entries_nsa = parse_nsa(nsa)
    assert entries_nsa and entries_nsa[0][0] == "nscript.dat", entries_nsa
    _n, off, msize, comp, unpacked = entries_nsa[0]
    assert comp == 2, f"压缩方式应为 LZSS(2)，实际 {comp}"
    assert unpacked == len(_build_script_bytes()), unpacked
    decoded = lzss_decode(nsa[off : off + msize], unpacked)
    assert decoded == _build_script_bytes(), "LZSS 解压结果与原文件不符"

    # 5) 端到端：目录 -> 发现 -> 抽取
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ns_selftest_"))
    try:
        (tmp / "nscript.dat").write_bytes(enc)
        (tmp / "arc.nsa").write_bytes(nsa)
        assert detect_dir(tmp) > 0, "detect_dir 应该认出 NScripter"
        found = dict(iter_scripts(tmp))
        assert "nscript.dat" in found, sorted(found)
        assert any(k.endswith("nscript.dat") and k.startswith("arc.nsa") for k in found), sorted(found)
        arc_key = next(k for k in found if k.startswith("arc.nsa"))
        arc_lines = extract_lines(arc_key, found[arc_key])
        assert arc_lines == _EXPECTED_TEXT, f"NSA 内脚本抽取不符：{arc_lines!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 6) 垃圾输入不炸
    assert extract_lines("junk.dat", b"\x00\x01\x02\x03") == []
    assert list(iter_scripts(tmp / "nope")) == []
    assert parse_sar(b"") == []
    assert parse_nsa(b"\x00") == []

    print("SELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

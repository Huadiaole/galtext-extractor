"""编码探测、文本清洗与对话抽取。

这一层是与引擎无关的：任何解析器把脚本字节交到这里，都能得到
干净的对话行。核心是两件事：

1. ``decode_auto`` —— 猜编码（UTF-8 / UTF-16 / Shift-JIS / GBK ...），
   用「像不像正常日文/中文文本」的字符评分来选，而不是简单 try/except。
2. ``extract_dialogue`` —— 从一行脚本里挖出真正的台词，
   先处理引号 ``「」``/``『』``/``""``，再退回整行判定，
   并顺手剥掉 KAG 标签、HTML 标签、控制字符。
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from typing import Iterable, Iterator

# --------------------------------------------------------------------------
# 字符评分表（BMP 65536 项，构建一次，查询 O(1)）
# --------------------------------------------------------------------------
_WEIGHT_OTHER = -8
_WEIGHT_GOOD = 1
_WEIGHT_GREAT = 3
_WEIGHT_BAD = -25
_WEIGHT_NEUTRAL = 0

# 这些是存进 bytearray 的哨兵值（bytearray 只能放 0-255）
_SENTINEL_BAD = 0xFF
_SENTINEL_PUA = 0xFA
_SENTINEL_HALFWIDTH = 0xFB
_SENTINEL_NEUTRAL = 0xFC

_TABLE = bytearray(65536)  # 0 = 未分类


def _mark(lo: int, hi: int, weight: int) -> None:
    if hi > 0xFFFF:
        hi = 0xFFFF
    _TABLE[lo : hi + 1] = bytes([weight & 0xFF]) * (hi - lo + 1)


def _build_table() -> None:
    # 空白与控制字符
    _mark(0x0000, 0x0008, _SENTINEL_BAD)
    _mark(0x0009, 0x000A, _WEIGHT_GOOD)
    _mark(0x000B, 0x000C, _SENTINEL_BAD)
    _mark(0x000D, 0x000D, _WEIGHT_GOOD)  # CR，UTF-16 的换行要用
    _mark(0x000E, 0x001F, _SENTINEL_BAD)
    _mark(0x007F, 0x009F, _SENTINEL_BAD)
    # ASCII 可打印
    _mark(0x0020, 0x007E, _WEIGHT_GOOD)
    # 拉丁补充 / 扩展
    _mark(0x00A0, 0x024F, _WEIGHT_GOOD)
    # 希腊 / 西里尔
    _mark(0x0370, 0x03FF, _WEIGHT_GOOD)
    _mark(0x0400, 0x04FF, _WEIGHT_GOOD)
    # 常用标点、箭头、圈号、几何图形、杂项符号
    _mark(0x2000, 0x206F, _WEIGHT_GOOD)
    _mark(0x2070, 0x209F, _WEIGHT_GOOD)
    _mark(0x20A0, 0x20CF, _WEIGHT_GOOD)
    _mark(0x2100, 0x214F, _WEIGHT_GOOD)
    _mark(0x2150, 0x218F, _WEIGHT_GOOD)
    _mark(0x2190, 0x21FF, _WEIGHT_GOOD)
    _mark(0x2200, 0x22FF, _WEIGHT_GOOD)
    _mark(0x2460, 0x24FF, _WEIGHT_GOOD)
    _mark(0x2500, 0x257F, _WEIGHT_GOOD)
    _mark(0x2580, 0x259F, _WEIGHT_GOOD)
    _mark(0x25A0, 0x25FF, _WEIGHT_GOOD)
    _mark(0x2600, 0x26FF, _WEIGHT_GOOD)
    _mark(0x2700, 0x27BF, _WEIGHT_GOOD)
    # CJK 标点 + 假名 + CJK 汉字
    _mark(0x3000, 0x303F, _WEIGHT_GOOD)
    _mark(0x3040, 0x309F, _WEIGHT_GREAT)  # 平假名
    _mark(0x30A0, 0x30FF, _WEIGHT_GREAT)  # 片假名
    _mark(0x3100, 0x312F, _WEIGHT_GREAT)  # 注音
    _mark(0x3130, 0x318F, _WEIGHT_GREAT)  # 谚文兼容
    _mark(0x31F0, 0x31FF, _WEIGHT_GREAT)  # 片假名扩展
    _mark(0x3200, 0x32FF, _WEIGHT_GREAT)  # 带圈 CJK
    _mark(0x3400, 0x4DBF, _WEIGHT_GREAT)  # CJK 扩展 A
    _mark(0x4E00, 0x9FFF, _WEIGHT_GREAT)  # CJK 基本区
    _mark(0xAC00, 0xD7AF, _WEIGHT_GREAT)  # 谚文音节
    _mark(0xF900, 0xFAFF, _WEIGHT_GREAT)  # CJK 兼容
    # 全角形式 / 半角片假名
    _mark(0xFF00, 0xFF65, _WEIGHT_GOOD)
    # 半角片假名单独标记：正常 galgame 文本里极少成片出现，
    # 一旦大量出现基本就是把 SJIS 当 GBK / GBK 当 SJIS 解码错了。
    _mark(0xFF66, 0xFF9F, _SENTINEL_HALFWIDTH)
    _mark(0xFFA0, 0xFFEF, _WEIGHT_GOOD)
    # 私用区：一些游戏用它放自定义字形，不算正常文本
    _mark(0xE000, 0xF8FF, _SENTINEL_PUA)
    # 替换字符 / 代理区
    _mark(0xD800, 0xDFFF, _SENTINEL_BAD)
    _mark(0xFFFD, 0xFFFD, _SENTINEL_BAD)
    _mark(0xFE00, 0xFE0F, _SENTINEL_NEUTRAL)  # 变体选择符
    _mark(0x200B, 0x200F, _SENTINEL_NEUTRAL)  # 零宽字符


_build_table()


def char_weight(ch: str) -> int:
    cp = ord(ch)
    if cp > 0xFFFF:
        return _WEIGHT_GREAT  # 增补平面基本都是 emoji / 汉字扩展，宽松处理
    w = _TABLE[cp]
    if w == 0:
        return _WEIGHT_OTHER
    if w == _SENTINEL_BAD:
        return _WEIGHT_BAD
    if w == _SENTINEL_PUA:
        return -6
    if w == _SENTINEL_HALFWIDTH:
        return -4
    if w == _SENTINEL_NEUTRAL:
        return _WEIGHT_NEUTRAL
    return w


# --------------------------------------------------------------------------
# 编码探测
# --------------------------------------------------------------------------
_ASCII_GUARD = 20  # 少于这么多字节的样本，尽量保守

_CANDIDATE_ENCODINGS = (
    "utf-8",
    "utf-16-le",
    "utf-16-be",
    "cp932",  # Shift-JIS (Windows 日文)
    "cp936",  # GBK (简中)
    "cp950",  # Big5 (繁中)
    "euc-jp",
    "cp1252",
)
# 说明：cp949(韩文) 被故意排除在自动探测之外。CP949 与 GBK 的双字节空间
# 几乎完全重叠，简中文本按 cp949 解出来会得到大片合法谚文，两者靠统计无法
# 可靠区分。需要韩文时用 extract(..., encoding="cp949") 显式指定。


def _score_text(text: str) -> float:
    """给一段解码结果打「像正常语言」的分。

    单看「字符是否合法」不足以区分 Shift-JIS 与 GBK —— 两者的双字节
    空间高度重叠，SJIS 假名按 GBK 解出来也是一片合法汉字。因此这里加两个
    语言层面的信号：

    * 平假名 / 全角片假名比例高  -> 强烈支持「这是日文，编码猜对了」
    * 半角片假名比例高          -> 强烈反对（几乎只出现在解码错误时）
    """
    if not text:
        return float("-inf")
    total = 0
    bad = 0
    hira = 0
    kata = 0
    halfwidth = 0
    replacement = 0
    for ch in text:
        cp = ord(ch)
        w = char_weight(ch)
        total += w
        if w <= _WEIGHT_BAD:
            bad += 1
        if cp == 0xFFFD:
            replacement += 1
        elif 0x3040 <= cp <= 0x309F:
            hira += 1
        elif 0x30A0 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            kata += 1
        elif 0xFF66 <= cp <= 0xFF9F:
            halfwidth += 1
    n = len(text)
    return (
        total / n
        + 9.0 * (hira / n)
        + 4.0 * (kata / n)
        - 14.0 * (halfwidth / n)
        - 30.0 * (bad / n)
        - 45.0 * (replacement / n)
    )


def _plausible_utf16(sample: bytes, enc: str) -> bool:
    """BOM 缺失时，判断这段字节像不像真的 UTF-16 文本。

    Shift-JIS / GBK 字节按 UTF-16 解读同样会得到一片「合法」码位（SJIS 的
    尾字节 0x4E-0x9F 正好落进 CJK 基本区），所以只看解码是否成功没有意义。
    真正的 UTF-16 文本里几乎每个码位都落在可读区间；错读的结果则会混入
    私用区（0xE000-0xF8FF）、代理区（0xD800-0xDFFF）和大量未分配码位。
    用「可读码位覆盖率」做闸门。
    """
    if len(sample) < 32:
        return False
    little = enc.endswith("le")
    units = 0
    readable = 0
    for i in range(0, len(sample) - 1, 2):
        b0, b1 = sample[i], sample[i + 1]
        low, high = (b0, b1) if little else (b1, b0)
        cp = low | (high << 8)
        if cp == 0:
            continue  # UTF-16 里的填充/ASCII 高位零，跳过不计
        units += 1
        if char_weight(chr(cp)) >= _WEIGHT_GOOD:
            readable += 1
    if units < 12:
        return False
    return readable / units >= 0.95


def decode_auto(data: bytes) -> tuple[str, str, float]:
    """返回 ``(文本, 编码名, 置信度)``。

    编码名可能是 ``"utf-8"`` 这类标准名，也可能是 ``"binary"``。
    """
    if not data:
        return "", "empty", 0.0

    # BOM 优先
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig", 1.0
    if data.startswith(b"\xff\xfe\x00\x00"):
        return data[4:].decode("utf-32-le", "replace"), "utf-32-le", 1.0
    if data.startswith(b"\x00\x00\xfe\xff"):
        return data[4:].decode("utf-32-be", "replace"), "utf-32-be", 1.0
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le", 1.0
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be", 1.0

    sample = data[:65536]

    # 严格 UTF-8 解码通过就基本可以定案：非 ASCII 的长字节串合法
    # UTF-8 是极小概率事件。
    try:
        strict_text = data.decode("utf-8")
    except UnicodeDecodeError:
        strict_text = None
    if strict_text is not None and (len(data) >= _ASCII_GUARD or data.isascii()):
        return strict_text, "utf-8", 1.0

    best_text, best_enc, best_score = "", "binary", float("-inf")

    for enc in _CANDIDATE_ENCODINGS:
        if enc == "utf-8":
            continue  # 上面已确认严格解码失败
        if enc.startswith("utf-16") and not _plausible_utf16(sample, enc):
            continue
        try:
            text = sample.decode(enc, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
        score = _score_text(text)
        if score > best_score:
            best_text, best_enc, best_score = text, enc, score

    if best_enc == "binary":
        return data.decode("latin-1", "replace"), "binary", 0.0

    full = data.decode(best_enc, "replace")
    confidence = max(0.0, min(1.0, (best_score + 12.0) / 30.0))
    return full, best_enc, confidence


def looks_like_text(data: bytes, threshold: float = 0.35) -> bool:
    """粗判一段字节是不是文本（用于决定要不要当作脚本来抽）。"""
    if not data:
        return False
    if b"\x00" in data[:4096] and not (
        data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff")
    ):
        # 含有大量 NUL 但又不是 UTF-16 的，大概率是二进制资源
        if data[:4096].count(0) > len(data[:4096]) * 0.25:
            return False
    text, enc, conf = decode_auto(data[:16384])
    if enc == "binary":
        return False
    if conf >= threshold:
        return True
    # 置信度低但包含成片日文/中文时也放行
    return has_japanese(text[:4096])


def has_japanese(text: str) -> bool:
    """是否含有假名或汉字（够判断对话文本）。"""
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            continue
        if _TABLE[cp] == _WEIGHT_GREAT:
            return True
    return False


def has_kana(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF or 0xFF66 <= cp <= 0xFF9F:
            return True
    return False


def has_fullwidth_kana(text: str) -> bool:
    """全角假名（平假名 / 片假名）。半角片假名不算 —— 那通常是解码错误。"""
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            return True
    return False


def has_cjk(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
            return True
    return False


_CJK_PUNCT = set("。、，．・「」『』（）〈〉《》【】！？：；…‥〜～—―　·")
_CJK_PUNCT_CODES = {ord(c) for c in _CJK_PUNCT}


def has_cjk_punct(text: str) -> bool:
    return any(ord(ch) in _CJK_PUNCT_CODES for ch in text)


def is_japanese_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF  # 假名
        or 0x31F0 <= cp <= 0x31FF
        or 0x3400 <= cp <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= cp <= 0x9FFF  # CJK 基本区
        or 0xF900 <= cp <= 0xFAFF
        or ord(ch) in _CJK_PUNCT_CODES
    )


def decode_with(data: bytes, encoding: str) -> tuple[str, str, float]:
    """按指定编码解码（用于 ``--encoding`` 之类的强制指定）。

    ``encoding`` 支持 ``auto``（等同于 :func:`decode_auto`）。
    """
    if not encoding or encoding.lower() in ("auto", "detect"):
        return decode_auto(data)
    try:
        return data.decode(encoding, "replace"), encoding, 1.0
    except LookupError:
        raise ValueError(f"未知编码：{encoding}") from None


# --------------------------------------------------------------------------
# 清洗
# --------------------------------------------------------------------------
# KAG 风格标签：[wait time=500] / [r] / [/ruby] / [if exp="..."]
_KAG_TAG_RE = re.compile(r"\[/?(?:[A-Za-z_][A-Za-z0-9_]*)(?:\s+[^\[\]\n]{0,200})?\]")
# 只有 ASCII 的尖括号标签：<br> <color=#fff> </font>
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s+[^<>\n]{0,120})?/?>")
# 形如 {0x1234} / %s 之类的占位或控制序列
_CTRL_SEQ_RE = re.compile(r"\\[nrt]|\{[0-9A-Fa-fxX,\s]{1,20}\}")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_WS_RE = re.compile(r"[ \t\u3000\u00a0]+")
# 连续重复标点压缩（「あああああ」保留，但！！！！压成！）
_REPEAT_PUNCT_RE = re.compile(r"([!?！？。、,.~〜ー…])\1{3,}")

# 一眼就是代码/指令的行首
_COMMAND_PREFIXES = (
    "*",
    ";",
    "//",
    "#",
    "@",
    "//",
    "::",
    "function",
    "function ",
    "var ",
    "int ",
    "float ",
    "if ",
    "else",
    "for ",
    "while ",
    "switch",
    "case ",
    "break",
    "continue",
    "return",
    "goto ",
    "class ",
    "struct ",
    "import ",
    "export ",
    "include",
    "#include",
    "#define",
    "const ",
    "let ",
    "typedef",
    "namespace ",
    "using ",
    "end",
    "endif",
    "endfunction",
    "endmacro",
    "macro ",
    "setvar",
    "getvar",
    "mov ",
    "jmp ",
    "call ",
    "push ",
    "pop ",
    "sf.",
    "kag.",
    "Script",
    "Storage",
    "Plugins",
    "System.",
    "Debug.",
    "Console.",
    "Window.",
    "Layer.",
    "Timer.",
    "Storages.",
    "include ",
    "loadplugin",
    "caption",
    "version",
    "define",
    "def ",
    "mesbox",
    "clickstr",
    "savegame",
    "textspeed",
    "automode",
    "effect",
    "bgm ",
    "bg ",
    "se ",
    "wait ",
    "ld ",
    "csp ",
    "dwave",
    "mp3loop",
    "play ",
    "stop",
    "resettimer",
    "goto",
)

_CODE_SYMBOLS = set("{}()[]<>=;:\\|&!$%^~`")


@dataclasses.dataclass(slots=True)
class CleanOptions:
    """文本抽取的调参集合，GUI 会把它暴露成控件。"""

    min_len: int = 2
    max_len: int = 400
    require_japanese: bool = True
    strip_tags: bool = True
    keep_narration: bool = True
    drop_ascii_only: bool = True
    aggressive: bool = False
    """激进模式：放宽代码行过滤，宁可多收也不漏（适合兜底扫描）。"""

    chinese_only: bool = False
    """只保留中文行：丢掉含有假名的行。

    用途是「原版 + 汉化补丁」并存的游戏 —— 两版脚本都在时，
    日文行的特征就是有假名，中文行没有，所以按假名一刀切能把原文滤掉。

    这是个**启发式**，不是语言识别：中文汉化里偶尔残留的假名（拟声词、
    日式称呼）也会被一起丢掉。需要保底时把它关掉即可。
    """

    track_speakers: bool = True
    """跨行跟踪说话人：记住 ``[name text="…"]`` 之类的声明，沿用到后续台词。

    很多 KAG 脚本不把名字写在同一行，而是先声明再跟一行纯台词。
    关掉它就只认「名字和台词在同一行」的写法。
    """

    guess_bare_speakers: bool = False
    """把「单独成行、短、无标点」的行当成角色名（名字与台词分行的脚本）。

    默认关闭：``翌日`` 这类短旁白和名字长得一模一样，开之前请先确认效果。
    """

    narration_speaker: str = ""
    """给旁白补的说话人标记（例如填 ``旁白``），让每行都有名字。

    留空则旁白不署名。标记在**所有说话人推断结束之后**才套用，
    所以不会影响「名字单独成行」的判断，也不会盖掉解析器给出的说话人。
    """

    def clone(self, **kwargs) -> "CleanOptions":
        data = dataclasses.asdict(self)
        data.update(kwargs)
        return CleanOptions(**data)


def normalize_text(s: str) -> str:
    s = _ZERO_WIDTH_RE.sub("", s)
    s = s.replace("\r", "").replace("\n", " ")
    s = _WS_RE.sub(" ", s)
    s = _REPEAT_PUNCT_RE.sub(r"\1\1", s)
    try:
        s = unicodedata.normalize("NFC", s)
    except Exception:  # pragma: no cover - 极端输入
        pass
    return s.strip()


def strip_tags(s: str) -> str:
    s = _KAG_TAG_RE.sub("", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _CTRL_SEQ_RE.sub("", s)
    return s


def _symbol_ratio(s: str) -> float:
    if not s:
        return 1.0
    bad = sum(1 for ch in s if ch in _CODE_SYMBOLS)
    return bad / len(s)


def looks_like_command(s: str) -> bool:
    """行首像脚本指令 / 代码吗？"""
    stripped = s.lstrip()
    if not stripped:
        return True
    low = stripped.lower()
    for prefix in _COMMAND_PREFIXES:
        p = prefix.lower()
        if low.startswith(p):
            # "end" 这类短前缀要防止误伤正常台词（台词里基本不会以 end 开头）
            if len(prefix) <= 4 and not stripped[len(prefix) : len(prefix) + 1] in (
                " ",
                "\t",
                "(",
                "=",
                ".",
                "",
            ):
                continue
            return True
    if stripped.startswith("</") or stripped.startswith("<?"):
        return True
    if "://" in stripped:
        return True
    return False


def _is_dialogue_shape(s: str, opts: CleanOptions) -> bool:
    if not s:
        return False
    if len(s) < opts.min_len or len(s) > opts.max_len:
        return False
    if opts.require_japanese and not has_japanese(s):
        return False
    # 「只保留中文」：日文行必定含假名，中文行基本不含 —— 按这个把原文滤掉
    if opts.chinese_only and has_kana(s):
        return False
    if opts.drop_ascii_only and s.isascii() and not opts.aggressive:
        return False
    if not opts.aggressive:
        if looks_like_command(s):
            return False
        sym = _symbol_ratio(s)
        if sym > 0.30:
            return False
        # 括号严重不配对，多半是代码
        if abs(s.count("(") - s.count(")")) > 1 or abs(s.count("{") - s.count("}")) > 1:
            return False
    else:
        if looks_like_command(s) and _symbol_ratio(s) > 0.4:
            return False
    # 至少要有一个假名或汉字，纯标点/纯数字不要
    if opts.require_japanese and not any(char_weight(ch) >= _WEIGHT_GREAT for ch in s):
        return False
    return True


# --------------------------------------------------------------------------
# 说话人识别
# --------------------------------------------------------------------------
# Name「台词」/ Name『台词』/ Name"台词"
_SPEAKER_QUOTE_RE = re.compile(
    r"^\s*(?P<name>[^「」『』\"“”\s:：,，。]{1,16})\s*[「『\"“](?P<body>.+?)[」』\"”]\s*$",
    re.DOTALL,
)
# Name：台词  /  Name: 台词
_SPEAKER_COLON_RE = re.compile(r"^\s*(?P<name>[^:：\s]{1,16})\s*[:：]\s*(?P<body>.+)$", re.DOTALL)
_QUOTE_ONLY_RE = re.compile(r"^\s*[「『\"“](?P<body>.+?)[」』\"”]\s*$", re.DOTALL)
# 台词里的引号片段
_INLINE_QUOTE_RE = re.compile(r"[「『](?P<body>[^「」『』\n]{1,400})[」』]")
_INLINE_DQUOTE_RE = re.compile(r"[\"“](?P<body>[^\"“”\n]{1,400})[\"”]")
# 「」里嵌了 ruby 之类标签被剥掉后可能留下空壳

_PLACEHOLDER_SPEAKERS = {
    "name",
    "speaker",
    "char",
    "chara",
    "text",
    "msg",
    "message",
    "str",
    "string",
    "label",
    "print",
    "echo",
    "mes",
    "say",
}


def split_speaker(s: str) -> tuple[str, str]:
    """拆出 ``(说话人, 台词)``；识别不出说话人时返回 ``("", s)``。"""
    m = _SPEAKER_QUOTE_RE.match(s)
    if m:
        name = m.group("name").strip()
        body = m.group("body").strip()
        if name and body and has_japanese(body) and name.lower() not in _PLACEHOLDER_SPEAKERS:
            return name, body
        if body:
            return "", body
    m = _QUOTE_ONLY_RE.match(s)
    if m:
        return "", m.group("body").strip()
    m = _SPEAKER_COLON_RE.match(s)
    if m:
        name = m.group("name").strip()
        body = m.group("body").strip()
        if (
            name
            and body
            and has_japanese(body)
            and name.lower() not in _PLACEHOLDER_SPEAKERS
            and not looks_like_command(name)
            and len(name) <= 12
        ):
            return name, body
    return "", s


def extract_dialogue(raw_line: str, opts: CleanOptions | None = None) -> list[tuple[str, str]]:
    """从一行脚本里抽取 ``(说话人, 台词)`` 列表。"""
    opts = opts or CleanOptions()
    line = raw_line.strip("\ufeff\r\n")
    if not line.strip():
        return []
    if opts.strip_tags:
        line = strip_tags(line)
    line = normalize_text(line)
    if not line:
        return []

    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(speaker: str, text: str) -> None:
        text = normalize_text(text)
        if not text or text in seen:
            return
        if not _is_dialogue_shape(text, opts):
            return
        seen.add(text)
        # 注意：这里**不**套用 narration_speaker。旁白标记是所有说话人推断
        # 都做完之后的最后一步（见 iter_dialogue_over_lines）——
        # 如果在这里就套上，它会被后续逻辑当成「真说话人」，
        # 既会让「名字单独成行」的前瞻失效，也会盖掉模块给出的说话人。
        results.append((normalize_text(speaker), text))

    # 1) 整行就是 Name「台词」或「台词」
    m = _SPEAKER_QUOTE_RE.match(line)
    if m:
        push(m.group("name"), m.group("body"))
        return results

    # 2) 行内引号片段（可能是代码行里嵌了字符串，也可能一行多句）
    inline = list(_INLINE_QUOTE_RE.finditer(line))
    for m in _INLINE_DQUOTE_RE.finditer(line):
        body = m.group("body")
        if has_japanese(body):
            inline.append(m)
    if inline:
        inline.sort(key=lambda mm: mm.start())
        for m in inline:
            body = m.group("body")
            prefix = line[: m.start()]
            speaker = ""
            if len(prefix) <= 16:
                cand = prefix.strip(" \t,，:：=({[")
                cand = re.sub(r"^[A-Za-z0-9_.\[\]]*\s*", "", cand).strip()
                if cand and has_japanese(cand) and not looks_like_command(cand):
                    speaker = cand
            push(speaker, body)
        if results:
            return results

    # 3) Name：台词
    m = _SPEAKER_COLON_RE.match(line)
    if m and has_japanese(m.group("body")):
        push(m.group("name"), m.group("body"))
        if results:
            return results

    # 4) 纯旁白
    if opts.keep_narration:
        push("", line)
    return results


# --------------------------------------------------------------------------
# 逐行驱动
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 跨行说话人跟踪
# --------------------------------------------------------------------------
#: KAG 这类引擎里，名字常常**不写在台词行**，而是先声明再跟一行纯台词：
#:
#:     [name text="悠斗"]
#:     おはよう、先輩。
#:
#: 只解析台词行的话，这些行的说话人就丢了 —— 所以需要记住「当前是谁在说」。
_SPEAKER_TAG_RE = re.compile(r"\[(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P<args>[^\]\n]*)\]")
_SPEAKER_ATTR_RE = re.compile(
    r"(?:text|name|value)\s*=\s*(?:\"(?P<d>[^\"]{1,24})\"|'(?P<s>[^']{1,24})'|(?P<b>[^\s\]]{1,24}))"
)
#: 这些标签的 ``name=`` / ``text=`` 属性视为「当前说话人」
_SPEAKER_TAGS = frozenset(
    {
        "name",
        "speaker",
        "chara",
        "chr",
        "char",
        "actor",
        "person",
        "chara_show",
        "chara_mod",
        "chara_new",
        "chara_face",
        "chara_ptext",
    }
)
#: 单独成行的名字不该包含这些字符
_BARE_NAME_BAD = set("。！？…、，．,.!?；;：:「」『』（）()[]{}<>\"'“”‘’·~～—–-")


def extract_speaker_declaration(line: str) -> str:
    """从一行脚本里找出「当前说话人」声明，找不到返回空串。

    支持 ``[name text="悠斗"]``、``[chara_mod name=悠斗]``、``[name 悠斗]`` 等写法。
    只在 :data:`_SPEAKER_TAGS` 里的标签会被认，避免把 ``[bg name="bg01"]``
    这种东西当成角色名。
    """
    if not line or "[" not in line:
        return ""
    for match in _SPEAKER_TAG_RE.finditer(line):
        tag = match.group("tag").lower()
        if tag not in _SPEAKER_TAGS:
            continue
        args = match.group("args")
        attr = _SPEAKER_ATTR_RE.search(args)
        if attr:
            value = (attr.group("d") or attr.group("s") or attr.group("b") or "").strip()
            if value and len(value) <= 24:
                return value
        # [name 悠斗] 这种位置参数写法
        positional = args.strip().strip("\"'")
        if positional and "=" not in positional and 0 < len(positional) <= 12:
            return positional
    return ""


def looks_like_bare_name(line: str) -> bool:
    """判断一行是不是「单独成行的角色名」。

    只在 :attr:`CleanOptions.guess_bare_speakers` 打开时使用 ——
    ``翌日`` 这类短旁白和名字长得一模一样，所以这条启发式默认关闭。
    """
    s = line.strip()
    if not (1 <= len(s) <= 8):
        return False
    if any(ch in _BARE_NAME_BAD for ch in s):
        return False
    if any(ch.isascii() for ch in s):
        return False
    return has_japanese(s)


def iter_dialogue_over_lines(
    items: Iterable[tuple[str, str]], opts: CleanOptions | None = None
) -> Iterator[tuple[int, str, str]]:
    """在**整段行序列**上抽取，产出 ``(行下标, 说话人, 台词)``（下标从 0 起）。

    ``items`` 是 ``(说话人提示, 原始行)`` —— 提示来自解析器模块（有些模块已经
    知道说话人了），为空则由这里推断。

    之所以要「整段」而不是逐行，是因为两种说话人写法都需要上下文：

    * ``[name text="悠斗"]`` 声明在**前一行**，要沿用到后面的台词；
    * 名字**单独成行**时，得看下一行是不是台词才能确认它是名字。
    """
    opts = opts or CleanOptions()
    rows = [((hint or ""), line) for hint, line in items]
    current = ""
    for index, (hint, raw) in enumerate(rows):
        if opts.track_speakers:
            declaration = extract_speaker_declaration(raw)
            if declaration:
                current = declaration

        if opts.guess_bare_speakers and looks_like_bare_name(raw):
            name = raw.strip()
            # 只有当**下一行是没有自己名字的台词**时，这一行才是「单独成行的名字」。
            # 若下一行自带说话人（``悠斗「…」``），那这一行更可能是旁白。
            for next_hint, following in rows[index + 1 :]:
                if not following.strip():
                    continue
                following_rows = extract_dialogue(following, opts)
                if next_hint.strip() or (
                    following_rows and not any(sp for sp, _body in following_rows)
                ):
                    current = name
                break
            if current == name:
                continue

        for speaker, body in extract_dialogue(raw, opts):
            # 优先级：解析器模块已经确定的说话人 > 台词行内自带的 > 跨行跟踪到的
            if not speaker:
                speaker = hint.strip() or (current if opts.track_speakers else "")
            # 最后一步才给旁白补标记：此时说话人推断已经全部结束，
            # 不会再影响「名字单独成行」的判断或模块给的名字。
            if not speaker and opts.narration_speaker:
                speaker = normalize_text(opts.narration_speaker)
            yield index, speaker, body


def iter_dialogue_lines(
    text: str, opts: CleanOptions | None = None, source: str = ""
) -> Iterator[tuple[int, str, str]]:
    """按行扫描一段脚本，产出 ``(行号, 说话人, 台词)``（行号从 1 起）。"""
    items = (("", line) for line in text.splitlines())
    for index, speaker, body in iter_dialogue_over_lines(items, opts):
        yield index + 1, speaker, body


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------
def dedupe_pairs(
    rows: Iterable[tuple[str, str]], keep_index: bool = False
) -> list[tuple[str, str]]:
    """按 ``(说话人, 台词)`` 去重，保持顺序。"""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for speaker, text in rows:
        key = (speaker, text)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# --------------------------------------------------------------------------
# 二进制里的字符串扫描（兜底用）
# --------------------------------------------------------------------------
_SJIS_LEAD = [(0x81, 0x9F), (0xE0, 0xFC)]


def scan_sjis_runs(data: bytes, min_chars: int = 4, max_gap: int = 1) -> list[str]:
    """在二进制里找 Shift-JIS 日文串（含半角片假名）。"""
    runs: list[str] = []
    buf = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        # 半角片假名 / ASCII 可打印 / 换行
        if 0xA1 <= b <= 0xDF or 0x20 <= b <= 0x7E:
            buf.append(b)
            i += 1
            continue
        if b in (0x0D, 0x0A):
            if len(buf) >= min_chars:
                runs.append(bytes(buf).decode("cp932", "replace"))
            buf.clear()
            i += 1
            continue
        # 双字节
        if any(lo <= b <= hi for lo, hi in _SJIS_LEAD):
            if i + 1 < n:
                t = data[i + 1]
                if 0x40 <= t <= 0x7E or 0x80 <= t <= 0xFC:
                    buf.append(b)
                    buf.append(t)
                    i += 2
                    continue
            if len(buf) >= min_chars:
                runs.append(bytes(buf).decode("cp932", "replace"))
            buf.clear()
            i += 1
            continue
        if len(buf) >= min_chars:
            runs.append(bytes(buf).decode("cp932", "replace"))
        buf.clear()
        i += 1
    if len(buf) >= min_chars:
        runs.append(bytes(buf).decode("cp932", "replace"))

    out = []
    for r in runs:
        r = r.strip()
        # 注意：这里不能要求「有假名」——纯中文文本一个假名都没有。
        # 质量把关交给后面的 _run_is_plausible。
        if len(r) >= min_chars and (
            has_fullwidth_kana(r) or has_cjk(r) or has_cjk_punct(r)
        ):
            out.append(r)
    return out


def scan_utf16_runs(data: bytes, min_chars: int = 4) -> list[str]:
    """在二进制里找 UTF-16LE 字符串。"""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(data)
    while i + 1 < n:
        cp = data[i] | (data[i + 1] << 8)
        ch = chr(cp)
        w = char_weight(ch) if cp else _WEIGHT_BAD
        if cp in (0x000A, 0x000D) or (cp and w >= _WEIGHT_GOOD and cp > 0x1F):
            if cp in (0x000A, 0x000D):
                if len(buf) >= min_chars:
                    out.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 2
            continue
        if len(buf) >= min_chars:
            out.append("".join(buf))
        buf = []
        i += 2
    if len(buf) >= min_chars:
        out.append("".join(buf))
    res = []
    for s in out:
        s = s.strip()
        if len(s) >= min_chars and (
            has_fullwidth_kana(s) or has_cjk(s) or has_cjk_punct(s)
        ):
            res.append(s)
    return res


def _run_is_plausible(s: str, min_chars: int, aggressive: bool = False) -> bool:
    """判断二进制扫描出来的一个「串」是真文本还是随机字节的巧合。

    随机字节流很容易凑出 4-8 个「合法」的 SJIS 双字节或 UTF-16 码位
    （CJK 基本区占了高位字节的一大块），所以必须再加语言层面的闸门：

    * 必须出现「锚点」——全角假名或中日文标点。日文对话必然有假名，
      中文对话必然有 。，！？ 或引号，随机数据几乎不会连续出现这些。
    * 日文字符占比要过半，挡掉「ASCII 里夹一个字」的噪声。
    """
    if len(s) < min_chars:
        return False
    n = len(s)
    halfwidth = 0
    private = 0
    jp = 0
    for ch in s:
        cp = ord(ch)
        if 0xFF66 <= cp <= 0xFF9F:
            halfwidth += 1
            continue
        if 0xE000 <= cp <= 0xF8FF:
            private += 1
            continue
        if char_weight(ch) <= _WEIGHT_BAD:
            return False
        if is_japanese_char(ch):
            jp += 1
    if halfwidth / n > 0.25 or private / n > 0.05:
        return False
    if not (has_fullwidth_kana(s) or has_cjk_punct(s)):
        return False
    if jp / n < (0.30 if aggressive else 0.50):
        return False
    return _score_text(s) >= (0.0 if aggressive else 2.0)


#: 允许出现在台词内部、但不参与「日文字符占比」统计的中性字符
_NEUTRAL_IN_TEXT = set(" \u3000\t0123456789０１２３４５６７８９!?！？.,，、-—~〜…・:：;；'\"“”()（）[]【】/／+＋")


def _japanese_segments(s: str, min_chars: int) -> list[str]:
    """把贪婪扫描出来的一长串切成「连续的日文片段」。

    二进制里字符串前后总是粘着垃圾字节，如果拿整串去做质量判定，
    中间那截好文本会被两头的乱码拖累而丢掉。切成片段后各判各的。
    """
    segments: list[str] = []
    current: list[str] = []
    for ch in s:
        if is_japanese_char(ch) or (current and ch in _NEUTRAL_IN_TEXT):
            current.append(ch)
        else:
            if len(current) >= min_chars:
                segments.append("".join(current).strip())
            current = []
    if len(current) >= min_chars:
        segments.append("".join(current).strip())
    return [seg for seg in segments if seg]


def scan_binary_strings(
    data: bytes, min_chars: int = 4, aggressive: bool = False
) -> list[str]:
    """通用的二进制字符串扫描，SJIS + UTF-16LE 都试。"""
    found = scan_sjis_runs(data, min_chars=min_chars)
    found.extend(scan_utf16_runs(data, min_chars=min_chars))

    cleaned: list[str] = []
    seen: set[str] = set()
    opts = CleanOptions(aggressive=True, min_len=min_chars)
    for raw in found:
        for segment in _japanese_segments(raw, min_chars):
            if not _run_is_plausible(segment, min_chars, aggressive):
                continue
            for _speaker, body in extract_dialogue(segment, opts):
                if not _run_is_plausible(body, min_chars, aggressive):
                    continue
                if body in seen:
                    continue
                seen.add(body)
                cleaned.append(body)
    return cleaned

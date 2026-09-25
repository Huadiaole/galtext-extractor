"""散装脚本文件解析器。

很多游戏（尤其是硬盘版、汉化版、解包后的版本）并不会把脚本塞进封包，
而是把 ``.ks`` / ``.txt`` / ``.ws2`` 之类的脚本散放在目录里。
这个模块负责把它们找出来并解码。

注意职责划分：本模块**不做**精度过滤，只负责
「找出像脚本的文件 -> 解码 -> 返回候选行」，
真正的标签清理、说话人拆分、长度/语言过滤统一由
:func:`galtext.parsers.call_extract_lines` 用用户指定的选项来做。
这样用户调参时不会因为模块内部写死了阈值而丢内容。
"""

from __future__ import annotations

import logging
import pathlib
from typing import Iterator

from .. import textkit

log = logging.getLogger(__name__)

ENGINE_ID = "plaintext"
ENGINE_NAME = "散装脚本文件"

#: 常见脚本/文本扩展名
SCRIPT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".ks",
        ".tjs",
        ".txt",
        ".ws2",
        ".mes",
        ".scn",
        ".sn",
        ".s",
        ".spt",
        ".asd",
        ".dat",
        ".ini",
        ".csv",
        ".tsv",
        ".lst",
        ".def",
        ".jaf",
        ".inc",
        ".mac",
        ".cfg",
        ".nscript",
        ".script",
        ".text",
    }
)

#: 这些目录里基本不会有脚本，跳过能省很多时间
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "bgm",
        "bg",
        "se",
        "voice",
        "cv",
        "movie",
        "video",
        "movie",
        "image",
        "images",
        "img",
        "cg",
        "ev",
        "chara",
        "sprite",
        "graphic",
        "graphics",
        "font",
        "fonts",
        "sound",
        "audio",
        "cache",
        "temp",
        "tmp",
        "savedata",
        "save",
        "screenshot",
        "screenshots",
        "system",
        "sys",
        "dll",
        "__pycache__",
        ".git",
        ".svn",
        "node_modules",
        "redist",
        "directx",
        "vcredist",
    }
)

DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_CANDIDATE_LINES = 400_000


def _walk(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """遍历目录（跳过明显的资源目录），产出文件路径。"""
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
    """找出散装脚本的数量，给出一个不高的置信度。"""
    root = pathlib.Path(root)
    if not root.is_dir():
        return 0
    hits = 0
    checked = 0
    for path in _walk(root):
        checked += 1
        if checked > 4000:
            break
        if path.suffix.lower() in SCRIPT_EXTENSIONS:
            hits += 1
            if hits >= 5:
                break
    if hits == 0:
        return 0
    # 故意压低：散装文件是「兜底」，不该盖过真正的引擎识别
    return min(25, 8 + hits * 3)


def iter_scripts(
    root: pathlib.Path, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> Iterator[tuple[str, bytes]]:
    """产出 ``(相对路径, 数据)``。"""
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    for path in _walk(root):
        if path.suffix.lower() not in SCRIPT_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > max_file_bytes:
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.debug("读取失败 %s: %s", path, exc)
            continue
        if not textkit.looks_like_text(data):
            continue
        try:
            virtual = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            virtual = path.name
        yield virtual, data


def guess_encoding(virtual_path: str, data: bytes, encoding: str = "auto") -> str:  # noqa: ARG001
    """返回这份文件被判定/指定的编码。"""
    try:
        return textkit.decode_with(data, encoding)[1]
    except ValueError:  # pragma: no cover
        return "?"


def extract_lines(virtual_path: str, data: bytes, encoding: str = "auto") -> list[str]:
    """返回候选文本行（未做精度过滤，由上层统一处理）。"""
    if not data:
        return []
    try:
        text, _enc, _conf = textkit.decode_with(data, encoding)
    except ValueError:
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 4000:
            continue
        lines.append(line)
        if len(lines) >= MAX_CANDIDATE_LINES:
            break
    return lines

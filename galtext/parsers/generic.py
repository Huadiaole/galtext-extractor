"""通用二进制扫描（最后兜底）。

当引擎识别不出来、或者封包格式没人写过解析器时，还有一招：
把文件当字节流，扫描里面成片的日文/中文字符串。

牺牲的是准确率与上下文（拿不到说话人、行序可能乱），
换来的是「几乎任何游戏都能捞出点东西」。

因为要遍历整个目录并且逐个文件扫描，代价较高，
默认**不启用**，需要用户在界面上勾选或 CLI 加 ``--include-generic``。
"""

from __future__ import annotations

import logging
import pathlib
from typing import Iterator

from .. import textkit

log = logging.getLogger(__name__)

ENGINE_ID = "generic"
ENGINE_NAME = "通用二进制扫描"

DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

#: 确定不可能有脚本的资源类型，直接跳过
SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".dds", ".ico",
        ".svg", ".psd", ".pic", ".anm", ".pna", ".dgm",
        ".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus", ".aac", ".wma", ".mid",
        ".mp4", ".avi", ".wmv", ".mkv", ".webm", ".mov", ".mpg", ".mpeg", ".rmvb",
        ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".cab", ".lzh",
        ".exe", ".dll", ".so", ".sys", ".ocx", ".msi",
        ".ttf", ".otf", ".ttc", ".fon", ".fot",
        ".db", ".sqlite", ".pdf", ".chm", ".hlp",
        ".xp3", ".arc", ".ypf", ".pfs", ".pak",  # 封包交给各自的解析器
    }
)


def detect_dir(root: pathlib.Path) -> int:  # noqa: ARG001
    """通用扫描永远不作为「识别出的引擎」。"""
    return 0


def iter_scripts(
    root: pathlib.Path,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> Iterator[tuple[str, bytes]]:
    """遍历所有可疑文件，产出 ``(相对路径, 数据)``。"""
    root = pathlib.Path(root)
    if not root.is_dir():
        return
    total = 0
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() in SKIP_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < 64 or size > max_file_bytes:
            continue
        if total + size > max_total_bytes:
            log.info("通用扫描达到总字节上限（%d），停止", max_total_bytes)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.debug("读取失败 %s: %s", path, exc)
            continue
        total += size
        try:
            virtual = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            virtual = path.name
        yield virtual, data


def extract_lines(
    virtual_path: str, data: bytes, aggressive: bool = True  # noqa: ARG001
) -> list[str]:
    """在二进制里扫描日文/中文串。"""
    try:
        return textkit.scan_binary_strings(data, min_chars=4, aggressive=aggressive)
    except Exception as exc:  # pragma: no cover - 兜底不该炸
        log.debug("通用扫描失败 %s: %s", virtual_path, exc)
        return []

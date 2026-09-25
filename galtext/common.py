"""公共数据类型。

所有解析器模块都遵循同一份契约：

    ENGINE_ID: str
    ENGINE_NAME: str

    def detect_dir(root: pathlib.Path) -> int          # 0-100 置信度
    def iter_scripts(root: pathlib.Path)               # 产出 (virtual_path, data)
    def extract_lines(virtual_path, data) -> list      # 产出对话文本

本模块只定义数据类型，不引入任何第三方依赖。
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any


@dataclasses.dataclass(slots=True)
class RawScript:
    """一份被成功还原出来的脚本数据。"""

    engine: str
    """产出它的解析器 ENGINE_ID。"""

    virtual_path: str
    """虚拟路径，例如 ``data.xp3/scenario/prologue.ks`` 或 ``Seen.txt``。"""

    data: bytes
    """脚本原始字节（已解密/解压）。"""

    origin: str = ""
    """它在磁盘上的来源文件绝对路径（封包成员则指向封包本身）。"""

    container: str = ""
    """封包名；散装文件为空字符串。"""

    @property
    def size(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "virtual_path": self.virtual_path,
            "origin": self.origin,
            "container": self.container,
            "size": self.size,
        }


@dataclasses.dataclass(slots=True)
class TextLine:
    """一条提取出来的对话/旁白。"""

    text: str
    source: str
    engine: str
    index: int = 0
    speaker: str = ""
    container: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "speaker": self.speaker,
            "text": self.text,
            "source": self.source,
            "engine": self.engine,
            "container": self.container,
        }

    def display(self) -> str:
        return f"{self.speaker}：{self.text}" if self.speaker else self.text


@dataclasses.dataclass(slots=True)
class ScriptReport:
    """一个脚本文件的处理结果，供 GUI 左栏展示。"""

    virtual_path: str
    engine: str
    origin: str = ""
    container: str = ""
    size: int = 0
    line_count: int = 0
    encoding: str = ""
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(slots=True)
class ScanResult:
    """一次完整扫描的产物。"""

    root: str = ""
    engines: list[tuple[str, int]] = dataclasses.field(default_factory=list)
    scripts: list[ScriptReport] = dataclasses.field(default_factory=list)
    lines: list[TextLine] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    cancelled: bool = False

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def best_engine(self) -> str:
        return self.engines[0][0] if self.engines else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "engines": [{"id": e, "score": s} for e, s in self.engines],
            "scripts": [s.to_dict() for s in self.scripts],
            "lines": [ln.to_dict() for ln in self.lines],
            "warnings": list(self.warnings),
        }


def human_size(n: int) -> str:
    """把字节数格式化成人类可读字符串。"""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def safe_relative(path: pathlib.Path, root: pathlib.Path) -> str:
    """尽量给出相对 root 的路径，失败时回退为绝对路径。"""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")

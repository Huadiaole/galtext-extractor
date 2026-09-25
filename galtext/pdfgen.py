"""最小 PDF 写入器（纯标准库）。

只实现这个项目真正需要的东西，但该守的规范一条不少：

* 正确的 xref 表 / trailer / startxref，任何阅读器都能打开；
* **Type0 + CIDFontType2 + Identity-H** 的 CJK 字体嵌入，
  配合 ``CIDToGIDMap /Identity``，所以写进内容流的 2 字节就是字形 ID；
* ``/ToUnicode`` CMap —— 没有它，PDF 里的中文复制出来是乱码、也搜不到；
* FlateDecode 压缩内容流与字体流。

坐标系统一用 PDF 的「点」（1/72 英寸），原点在左下角。
"""

from __future__ import annotations

import logging
import zlib
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

#: A4（点）
A4 = (595.276, 841.89)

#: 字体宽度表的基准：PDF 里字形宽度统一按 1000 单位/em 表示
TEXT_UNITS = 1000.0


def _num(value: float) -> str:
    """PDF 数字：去掉多余小数位，避免文件里全是浮点噪声。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _escape_name(name: str) -> str:
    """PDF 名字对象里只允许 ASCII 可见字符，其余一律换成下划线。"""
    out = []
    for ch in name:
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    result = "".join(out).strip("_")
    return result or "EmbeddedFont"


def _text_string(value: str) -> str:
    """把 Python 字符串写成 PDF 的 UTF-16BE 十六进制字符串对象（返回 ASCII 文本）。"""
    data = value.encode("utf-16-be", "replace")
    return "<FEFF" + data.hex().upper() + ">"


class PdfObjectStore:
    """对象仓库；对象号从 1 开始，``objects[0]`` 永远留空。"""

    def __init__(self) -> None:
        self.objects: list[bytes | None] = [None]

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects) - 1

    def put(self, number: int, payload: bytes) -> None:
        self.objects[number] = payload

    def add(self, payload: bytes) -> int:
        self.objects.append(payload)
        return len(self.objects) - 1

    def add_stream(self, dictionary: str, data: bytes, compress: bool = True) -> int:
        body = zlib.compress(data, 6) if compress else data
        extra = " /Filter /FlateDecode" if compress else ""
        payload = (
            f"<< {dictionary}{extra} /Length {len(body)} >>\nstream\n".encode("ascii")
            + body
            + b"\nendstream"
        )
        return self.add(payload)

    def serialize(self) -> tuple[bytes, list[int]]:
        """返回 ``(文件内容, 每个对象的字节偏移)``。"""
        out = bytearray()
        offsets = [0] * len(self.objects)
        for number in range(1, len(self.objects)):
            payload = self.objects[number]
            if payload is None:  # 理论上不该发生
                payload = b"<< >>"
            offsets[number] = len(out)
            out += f"{number} 0 obj\n".encode("ascii")
            out += payload
            out += b"\nendobj\n"
        return bytes(out), offsets


class PdfFont:
    """把一个 TrueType 字体（子集化后）嵌进 PDF，并负责文本编码与测宽。"""

    def __init__(
        self,
        store: PdfObjectStore,
        ttf,
        chars: Iterable[str],
        resource_name: str = "F1",
    ) -> None:
        self.ttf = ttf
        self.store = store
        self.resource_name = resource_name
        self._gid_cache: dict[str, int] = {}
        self.missing: set[str] = set()

        used_chars = {ch for ch in chars if ch and ch != "\x00"}
        used_gids: set[int] = set()
        self._gid_to_char: dict[int, str] = {}
        for ch in sorted(used_chars):
            gid = self.glyph_id(ch)
            if gid == 0:
                self.missing.add(ch)
                continue
            used_gids.add(gid)
            self._gid_to_char.setdefault(gid, ch)

        self.used_gids = used_gids
        self._build(ttf)

    # ------------------------------------------------------------------
    def glyph_id(self, ch: str) -> int:
        gid = self._gid_cache.get(ch)
        if gid is None:
            try:
                gid = int(self.ttf.glyph_id(ch))
            except Exception:  # pragma: no cover - 字体异常时按缺字处理
                gid = 0
            self._gid_cache[ch] = gid
        return gid

    @property
    def units_per_em(self) -> float:
        try:
            return float(self.ttf.units_per_em) or 1000.0
        except Exception:  # pragma: no cover
            return 1000.0

    def text_width(self, text: str, size: float) -> float:
        """一段文本在给定字号下的宽度（点）。"""
        total = 0
        for ch in text:
            gid = self.glyph_id(ch)
            try:
                total += float(self.ttf.advance_width(gid))
            except Exception:  # pragma: no cover
                total += self.units_per_em
        return total / self.units_per_em * size

    def encode(self, text: str) -> bytes:
        """把文本编码成内容流里用的十六进制字符串（每个字形 2 字节）。"""
        parts: list[str] = []
        for ch in text:
            gid = self.glyph_id(ch)
            if gid == 0:
                continue  # 缺字就不画，避免画出方框噪声
            parts.append(f"{gid & 0xFFFF:04X}")
        return ("<" + "".join(parts) + ">").encode("ascii")

    # ------------------------------------------------------------------
    def _build(self, ttf) -> None:
        try:
            subset_bytes = ttf.subset(self.used_gids)
        except Exception as exc:
            raise ValueError(f"字体子集化失败：{exc}") from exc

        family = _escape_name(str(getattr(ttf, "family", "") or "EmbeddedCJK"))
        # 子集字体按惯例用 6 个大写字母前缀 + 原字体名
        tag = _subset_tag(self.used_gids)
        base_font = f"{tag}+{family}"

        font_file = self.store.add_stream(
            f"/Length1 {len(subset_bytes)}", subset_bytes, compress=True
        )

        upem = self.units_per_em
        scale = TEXT_UNITS / upem
        try:
            ascent = float(ttf.ascent) * scale
            descent = float(ttf.descent) * scale
        except Exception:  # pragma: no cover
            ascent, descent = 880.0, -220.0
        bbox = [
            _num(0),
            _num(min(descent, -50)),
            _num(TEXT_UNITS),
            _num(max(ascent, 500)),
        ]

        descriptor = self.store.add(
            (
                "<< /Type /FontDescriptor"
                f" /FontName /{base_font}"
                " /Flags 4"
                f" /FontBBox [{' '.join(bbox)}]"
                " /ItalicAngle 0"
                f" /Ascent {_num(ascent)}"
                f" /Descent {_num(descent)}"
                f" /CapHeight {_num(ascent * 0.7)}"
                " /StemV 80"
                f" /FontFile2 {font_file} 0 R"
                " >>"
            ).encode("ascii")
        )

        widths = self._widths_array(scale)
        cid_font = self.store.add(
            (
                "<< /Type /Font /Subtype /CIDFontType2"
                f" /BaseFont /{base_font}"
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
                f" /FontDescriptor {descriptor} 0 R"
                f" /DW {_num(TEXT_UNITS)}"
                f" /W [{widths}]"
                " /CIDToGIDMap /Identity"
                " >>"
            ).encode("ascii")
        )

        to_unicode = self.store.add_stream("", self._to_unicode_cmap(), compress=True)

        self.object_number = self.store.add(
            (
                "<< /Type /Font /Subtype /Type0"
                f" /BaseFont /{base_font}"
                " /Encoding /Identity-H"
                f" /DescendantFonts [{cid_font} 0 R]"
                f" /ToUnicode {to_unicode} 0 R"
                " >>"
            ).encode("ascii")
        )

    def _widths_array(self, scale: float) -> str:
        """``/W`` 数组：按字形 ID 升序，连续字形合并成 ``start [w w w]``。"""
        if not self.used_gids:
            return ""
        chunks: list[str] = []
        ordered = sorted(self.used_gids)
        run_start = ordered[0]
        run_widths: list[str] = []
        previous = ordered[0]
        for gid in ordered:
            try:
                width = float(self.ttf.advance_width(gid)) * scale
            except Exception:  # pragma: no cover
                width = TEXT_UNITS
            if gid != previous + 1:
                chunks.append(f"{run_start} [{' '.join(run_widths)}]")
                run_start = gid
                run_widths = []
            run_widths.append(_num(width))
            previous = gid
        if run_widths:
            chunks.append(f"{run_start} [{' '.join(run_widths)}]")
        return " ".join(chunks)

    def _to_unicode_cmap(self) -> bytes:
        """生成 ToUnicode CMap，让 PDF 里的中文可复制、可搜索。"""
        lines = [
            "/CIDInit /ProcSet findresource begin",
            "12 dict begin",
            "begincmap",
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            "/CMapName /Adobe-Identity-UCS def",
            "/CMapType 2 def",
            "1 begincodespacerange",
            "<0000> <FFFF>",
            "endcodespacerange",
        ]
        items = sorted(self._gid_to_char.items())
        for start in range(0, len(items), 100):
            block = items[start : start + 100]
            lines.append(f"{len(block)} beginbfchar")
            for gid, ch in block:
                try:
                    utf16 = ch.encode("utf-16-be").hex().upper()
                except Exception:  # pragma: no cover
                    continue
                if not utf16:
                    continue
                lines.append(f"<{gid & 0xFFFF:04X}> <{utf16}>")
            lines.append("endbfchar")
        lines += [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
        ]
        return ("\n".join(lines) + "\n").encode("ascii")


def _subset_tag(gids: Iterable[int]) -> str:
    """由字形集合算一个稳定的 6 字母前缀（子集字体的命名惯例）。"""
    seed = 0
    for gid in sorted(gids):
        seed = (seed * 131 + gid) & 0xFFFFFFFF
    letters = []
    for _ in range(6):
        letters.append(chr(ord("A") + seed % 26))
        seed //= 26
    return "".join(letters)


class PdfPage:
    """一页的内容流构造器。"""

    def __init__(self, document: "PdfDocument", width: float, height: float) -> None:
        self.document = document
        self.width = width
        self.height = height
        self.ops: list[bytes] = []

    # ------------------------------------------------------------------
    def text(
        self,
        x: float,
        y: float,
        content: str,
        size: float,
        color: tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        """在 ``(x, y)`` 画一段文本（``y`` 是基线位置）。"""
        if not content:
            return
        encoded = self.document.font.encode(content)
        if encoded == b"<>":
            return
        r, g, b = color
        self.ops.append(
            b"BT "
            + f"/{self.document.font.resource_name} {_num(size)} Tf ".encode("ascii")
            + f"{_num(r)} {_num(g)} {_num(b)} rg ".encode("ascii")
            + f"1 0 0 1 {_num(x)} {_num(y)} Tm ".encode("ascii")
            + encoded
            + b" Tj ET"
        )

    def text_centered(
        self,
        y: float,
        content: str,
        size: float,
        color: tuple[float, float, float] = (0, 0, 0),
        left: float = 0.0,
        right: float | None = None,
    ) -> None:
        right = self.width if right is None else right
        width = self.document.font.text_width(content, size)
        self.text(left + ((right - left) - width) / 2.0, y, content, size, color)

    def text_right(
        self,
        x_right: float,
        y: float,
        content: str,
        size: float,
        color: tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        width = self.document.font.text_width(content, size)
        self.text(x_right - width, y, content, size, color)

    def rule(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        width: float = 0.6,
        color: tuple[float, float, float] = (0.75, 0.75, 0.75),
    ) -> None:
        r, g, b = color
        self.ops.append(
            (
                f"q {_num(r)} {_num(g)} {_num(b)} RG {_num(width)} w "
                f"{_num(x1)} {_num(y1)} m {_num(x2)} {_num(y2)} l S Q"
            ).encode("ascii")
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        color: tuple[float, float, float] = (0.9, 0.9, 0.9),
    ) -> None:
        r, g, b = color
        self.ops.append(
            (
                f"q {_num(r)} {_num(g)} {_num(b)} rg "
                f"{_num(x)} {_num(y)} {_num(w)} {_num(h)} re f Q"
            ).encode("ascii")
        )

    def stream(self) -> bytes:
        return b"\n".join(self.ops) + (b"\n" if self.ops else b"")


class PdfDocument:
    """一份 PDF。用法：建文档 -> 反复 ``new_page()`` -> ``save()``。"""

    def __init__(
        self,
        ttf,
        chars: Iterable[str],
        title: str = "",
        author: str = "",
        creator: str = "GalText Extractor",
        page_size: tuple[float, float] = A4,
    ) -> None:
        self.store = PdfObjectStore()
        self.page_size = page_size
        self.width, self.height = page_size
        self.font = PdfFont(self.store, ttf, chars)
        self.pages: list[tuple[int, PdfPage]] = []

        self._catalog = self.store.reserve()
        self._pages_node = self.store.reserve()
        self._info = self.store.add(
            (
                "<< /Title "
                + _text_string(title or "GalText 剧本")
                + " /Author "
                + _text_string(author or "")
                + " /Creator "
                + _text_string(creator)
                + " /Producer "
                + _text_string(creator)
                + " >>"
            ).encode("ascii")
        )

    # ------------------------------------------------------------------
    def new_page(self) -> PdfPage:
        page = PdfPage(self, self.width, self.height)
        page_number = self.store.reserve()
        self.pages.append((page_number, page))
        return page

    def save(self, path) -> None:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 每页的 Contents 流
        for index, (page_number, page) in enumerate(self.pages):
            content = self.store.add_stream("", page.stream(), compress=True)
            self.store.put(
                page_number,
                (
                    "<< /Type /Page"
                    f" /Parent {self._pages_node} 0 R"
                    f" /MediaBox [0 0 {_num(self.width)} {_num(self.height)}]"
                    f" /Resources << /Font << /{self.font.resource_name} "
                    f"{self.font.object_number} 0 R >> >>"
                    f" /Contents {content} 0 R"
                    " >>"
                ).encode("ascii"),
            )

        kids = " ".join(f"{number} 0 R" for number, _ in self.pages)
        self.store.put(
            self._pages_node,
            (
                f"<< /Type /Pages /Count {len(self.pages)} /Kids [{kids}] >>"
            ).encode("ascii"),
        )
        self.store.put(
            self._catalog,
            f"<< /Type /Catalog /Pages {self._pages_node} 0 R >>".encode("ascii"),
        )

        body, offsets = self.store.serialize()
        total = len(self.store.objects)

        header = b"%PDF-1.7\n"
        # 二进制标记：告诉工具这是二进制文件，别按文本处理
        header += b"%\xe2\xe3\xcf\xd3\n"

        out = bytearray()
        out += header
        out += body

        # xref 里必须是**绝对**文件偏移，所以要把文件头的长度补上。
        # 少加这一点，阅读器就会从错误的位置找对象，直接报「文件已损坏」。
        base = len(header)
        xref_offset = len(out)
        out += f"xref\n0 {total}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for number in range(1, total):
            out += f"{offsets[number] + base:010d} 00000 n \n".encode("ascii")

        out += (
            f"trailer\n<< /Size {total} /Root {self._catalog} 0 R "
            f"/Info {self._info} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")

        target.write_bytes(bytes(out))
        log.info(
            "PDF 已写入 %s：%d 页，%d 个对象，%d 字节",
            target,
            len(self.pages),
            total - 1,
            len(out),
        )

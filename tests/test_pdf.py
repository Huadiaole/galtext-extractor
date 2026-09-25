"""剧本 PDF 的测试。

分两层：

* **结构层**用假字体跑，不依赖机器上装了什么字体 —— 验证 PDF 语法
  （xref 偏移、对象完整性、流长度、字体对象齐不齐）与排版逻辑
  （禁则换行、分页、缺字处理）。
* **真实层**用系统里的中文字体跑，验证字形子集化确实把文件压小了、
  并且子集字体能被重新解析。机器上没有中文字体时自动跳过。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from galtext import pdfgen, scriptpdf  # noqa: E402
from galtext.common import ScanResult, ScriptReport, TextLine  # noqa: E402


class FakeFont:
    """鸭子类型的假字体：pdfgen/scriptpdf 只用到这几个方法。

    默认「来者不拒」——任何字符都给一个字形 ID，模拟一个覆盖度很好的真实字体；
    ``strict=True`` 时只认构造时给定的字符，用来测缺字处理。

    ``subset`` 返回占位字节 —— 结构层测试不检查嵌入字体能否渲染，
    那部分交给 RealFontTests。
    """

    units_per_em = 1000
    ascent = 880
    descent = -220
    family = "FakeCJK"
    is_cff = False

    def __init__(self, chars: str = "", strict: bool = False) -> None:
        self.strict = strict
        self._map: dict[str, int] = {}
        for ch in chars:
            if ch not in self._map:
                self._map[ch] = len(self._map) + 1

    @property
    def num_glyphs(self) -> int:
        return len(self._map) + 1

    def glyph_id(self, ch: str) -> int:
        gid = self._map.get(ch)
        if gid is not None:
            return gid
        if self.strict:
            return 0
        gid = len(self._map) + 1
        self._map[ch] = gid
        return gid

    def advance_width(self, gid: int) -> int:
        return 1000 if gid else 0

    def text_width(self, text: str, size: float) -> float:
        return len(text) * size

    def subset(self, gids) -> bytes:
        return b"\x00\x01\x00\x00" + b"\x00" * 124  # 占位，128 字节

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 一个够用的 PDF 结构解析器
# --------------------------------------------------------------------------
class PdfProbe:
    """把生成的 PDF 拆开检查，验证它是不是一份语法正确的文件。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        if not data.startswith(b"%PDF-"):
            raise AssertionError("缺少 %PDF- 头")
        marker = data.rfind(b"startxref")
        if marker < 0:
            raise AssertionError("缺少 startxref")
        self.xref_offset = int(data[marker + 9 :].split()[0])
        if data[self.xref_offset : self.xref_offset + 4] != b"xref":
            raise AssertionError("startxref 没有指向 xref 表")

        header = data[self.xref_offset :].split(b"\n")[1]
        self.count = int(header.split()[1])
        self.offsets: list[int] = []
        cursor = self.xref_offset + len(b"xref\n") + len(header) + 1
        for _ in range(self.count):
            entry = data[cursor : cursor + 20]
            self.offsets.append(int(entry[:10]))
            cursor += 20

        trailer = data[data.rfind(b"trailer") :]
        self.size = int(re.search(rb"/Size\s+(\d+)", trailer).group(1))  # type: ignore[union-attr]
        self.root = int(re.search(rb"/Root\s+(\d+)", trailer).group(1))  # type: ignore[union-attr]
        self.objects = self._parse_objects()

    def _parse_objects(self) -> dict[int, bytes]:
        objects: dict[int, bytes] = {}
        for number in range(1, self.count):
            start = self.offsets[number]
            if start <= 0:
                continue
            head = self.data[start : start + 40]
            if not head.startswith(f"{number} 0 obj".encode()):
                raise AssertionError(f"对象 {number} 的偏移不对：{head[:24]!r}")
            end = self.data.find(b"endobj", start)
            objects[number] = self.data[start:end]
        return objects

    def streams(self) -> list[tuple[bytes, bytes]]:
        """返回 ``[(字典, 解压后的流内容)]``。"""
        import zlib

        out = []
        for payload in self.objects.values():
            at = payload.find(b"stream\n")
            if at < 0:
                continue
            dictionary = payload[:at]
            declared = int(re.search(rb"/Length\s+(\d+)", dictionary).group(1))  # type: ignore[union-attr]
            body = payload[at + len(b"stream\n") :]
            body = body[: declared]
            if b"/FlateDecode" in dictionary:
                body = zlib.decompress(body)
            out.append((dictionary, body))
        return out


def sample_lines(count: int, source: str = "scenario/prologue.ks") -> list[TextLine]:
    rows: list[TextLine] = []
    speakers = ["悠斗", "先辈", ""]
    bodies = [
        "早上好，前辈。今天天气也不错呢。",
        "天空湛蓝清澈，校园里飘着樱花。",
        "「这里是什么地方……？」我低声自语道。",
        "A very long English sentence that must be wrapped without breaking words apart.",
    ]
    for index in range(count):
        rows.append(
            TextLine(
                text=bodies[index % len(bodies)],
                speaker=speakers[index % len(speakers)],
                source=source,
                engine="kirikiri",
                index=index + 1,
            )
        )
    return rows


# --------------------------------------------------------------------------
class WrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.font = FakeFont("".join(chr(c) for c in range(0x20, 0x7F)) + "早上好，前辈。「」（）")

    def test_empty(self) -> None:
        self.assertEqual(scriptpdf.wrap_text("", self.font, 10, 100), [])
        self.assertEqual(scriptpdf.wrap_text("   ", self.font, 10, 100), [])

    def test_single_line_when_it_fits(self) -> None:
        lines = scriptpdf.wrap_text("早上好", self.font, 10, 1000)
        self.assertEqual(lines, ["早上好"])

    def test_every_line_within_width(self) -> None:
        text = "早上好，前辈。「今天天气也不错呢。」天空湛蓝清澈。"
        lines = scriptpdf.wrap_text(text, self.font, 10, 80)  # 每行最多 8 字
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(self.font.text_width(line, 10), 80)
        # 不能丢字
        self.assertEqual("".join(lines), text)

    def test_no_line_starts_with_forbidden_punctuation(self) -> None:
        text = "他说「今天」很好，然后笑了笑。又补充了一句。"
        for width in range(40, 200, 10):
            for line in scriptpdf.wrap_text(text, self.font, 10, width):
                self.assertNotIn(
                    line[0],
                    scriptpdf.NO_LINE_START,
                    f"行首不该出现 {line[0]!r}（宽度 {width}）",
                )

    def test_no_line_ends_with_opening_bracket(self) -> None:
        text = "他说道（这是很重要的提示）然后继续走下去。"
        for width in range(40, 200, 10):
            for line in scriptpdf.wrap_text(text, self.font, 10, width):
                self.assertNotIn(
                    line[-1],
                    scriptpdf.NO_LINE_END,
                    f"行尾不该出现 {line[-1]!r}（宽度 {width}）",
                )

    def test_english_word_not_split(self) -> None:
        text = "prefix extraordinary supercalifragilistic tail"
        # 宽度要够放下最长的那个词，否则「不拆词」根本做不到
        lines = scriptpdf.wrap_text(text, self.font, 10, 260)
        self.assertGreater(len(lines), 1)
        for word in text.split():
            self.assertTrue(
                any(word in line for line in lines),
                f"英文单词 {word!r} 被从中间截断了：{lines}",
            )

    def test_word_longer_than_line_is_split_but_nothing_is_lost(self) -> None:
        """单词比整行还宽时只能拆开，但绝不能丢字。"""
        text = "a supercalifragilisticexpialidocious b"
        lines = scriptpdf.wrap_text(text, self.font, 10, 60)
        self.assertTrue(lines)
        self.assertEqual(
            "".join(lines).replace(" ", ""), text.replace(" ", "")
        )

    def test_very_long_unbreakable_token_still_returns_something(self) -> None:
        text = "超" * 50
        lines = scriptpdf.wrap_text(text, self.font, 10, 60)
        self.assertTrue(lines)
        self.assertEqual("".join(lines), text)


class SceneTests(unittest.TestCase):
    def test_grouping(self) -> None:
        rows = [
            TextLine(text="a", source="one.ks", engine="k", index=1),
            TextLine(text="b", source="one.ks", engine="k", index=2),
            TextLine(text="c", source="two.ks", engine="k", index=3),
            TextLine(text="d", source="one.ks", engine="k", index=4),
        ]
        scenes = scriptpdf.group_scenes(rows)
        self.assertEqual([len(s.lines) for s in scenes], [2, 1, 1])
        self.assertEqual([s.index for s in scenes], [1, 2, 3])
        self.assertEqual(scenes[0].label, "one.ks")

    def test_empty(self) -> None:
        self.assertEqual(scriptpdf.group_scenes([]), [])


class StructureTests(unittest.TestCase):
    """用假字体检查 PDF 语法与排版骨架。"""

    def setUp(self) -> None:
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_pdf_"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pdf_structure_is_valid(self) -> None:
        lines = sample_lines(120)
        target = self._tmp / "out.pdf"
        result = scriptpdf._build(
            lines,
            target,
            scriptpdf.ScriptPdfOptions(title="测试剧本"),
            None,
            FakeFont(),
            "FakeCJK",
            "fake.ttf",
        )
        probe = PdfProbe(target.read_bytes())

        self.assertGreaterEqual(probe.count, 5)
        self.assertIn(probe.root, probe.objects)
        self.assertIn(b"/Type /Catalog", probe.objects[probe.root])
        pages_ref = int(
            re.search(rb"/Pages\s+(\d+)\s+0\s+R", probe.objects[probe.root]).group(1)  # type: ignore[union-attr]
        )
        pages_payload = probe.objects[pages_ref]
        self.assertIn(b"/Type /Pages", pages_payload)
        count = int(re.search(rb"/Count\s+(\d+)", pages_payload).group(1))  # type: ignore[union-attr]
        self.assertEqual(count, result.pages)
        self.assertEqual(len(result.missing_chars), 0)

    def test_font_objects_present_and_correct(self) -> None:
        lines = sample_lines(20)
        target = self._tmp / "font.pdf"
        scriptpdf._build(
            lines, target, scriptpdf.ScriptPdfOptions(),
            None,
            FakeFont(),
            "FakeCJK", "fake.ttf",
        )
        blob = target.read_bytes()
        self.assertIn(b"/Subtype /Type0", blob)
        self.assertIn(b"/Encoding /Identity-H", blob)
        self.assertIn(b"/Subtype /CIDFontType2", blob)
        self.assertIn(b"/CIDToGIDMap /Identity", blob)
        self.assertIn(b"/FontFile2", blob)
        self.assertIn(b"/ToUnicode", blob)
        self.assertIn(b"/Filter /FlateDecode", blob)

    def test_stream_lengths_match_declared(self) -> None:
        """流长度写错是最容易让阅读器直接罢工的错误，必须逐条核对。"""
        lines = sample_lines(60)
        target = self._tmp / "streams.pdf"
        scriptpdf._build(
            lines, target, scriptpdf.ScriptPdfOptions(),
            None,
            FakeFont(),
            "FakeCJK", "fake.ttf",
        )
        data = target.read_bytes()
        # PdfProbe.streams() 会按声明的长度切片并解压；长度写错就会抛异常
        streams = PdfProbe(data).streams()
        self.assertGreaterEqual(len(streams), 2)
        for dictionary, body in streams:
            self.assertTrue(body is not None)

    def test_pagination_scales_with_content(self) -> None:
        pages = []
        for count in (5, 60, 300):
            lines = sample_lines(count)
            target = self._tmp / f"p{count}.pdf"
            result = scriptpdf._build(
                lines, target, scriptpdf.ScriptPdfOptions(),
                None,
                FakeFont(),
                "FakeCJK", "fake.ttf",
            )
            pages.append(result.pages)
        self.assertEqual(pages, sorted(pages))
        self.assertGreater(pages[-1], pages[0])

    def test_every_page_has_contents_and_footer(self) -> None:
        lines = sample_lines(150)
        target = self._tmp / "pages.pdf"
        result = scriptpdf._build(
            lines, target, scriptpdf.ScriptPdfOptions(),
            None,
            FakeFont(),
            "FakeCJK", "fake.ttf",
        )
        probe = PdfProbe(target.read_bytes())
        page_objects = [p for p in probe.objects.values() if b"/Type /Page" in p and b"/Type /Pages" not in p]
        self.assertEqual(len(page_objects), result.pages)
        for payload in page_objects:
            self.assertIn(b"/Contents", payload)
            self.assertIn(b"/MediaBox", payload)

        # 页码文字应该出现在内容流里（"1 / N" 的分页标记）
        joined = b"".join(body for _d, body in probe.streams())
        self.assertIn(b"1", joined)

    def test_missing_chars_are_reported_not_crashed(self) -> None:
        lines = [
            TextLine(text="这些字有：甲乙丙丁", speaker="某人", source="a.ks", engine="k", index=1)
        ]
        # strict 模式只认给定的几个字符，其余一律算缺字
        narrow = FakeFont("某人的甲", strict=True)
        target = self._tmp / "missing.pdf"
        result = scriptpdf._build(
            lines, target, scriptpdf.ScriptPdfOptions(), None, narrow, "Narrow", "narrow.ttf"
        )
        self.assertTrue(result.missing_chars)
        self.assertIn("乙", result.missing_chars)
        self.assertTrue(target.exists())

    def test_no_lines_raises(self) -> None:
        with self.assertRaises(ValueError):
            scriptpdf.build_pdf([], self._tmp / "empty.pdf")

    def test_scene_per_page(self) -> None:
        rows = []
        for scene in range(4):
            for i in range(6):
                rows.append(
                    TextLine(
                        text=f"第{scene}场第{i}句台词内容。",
                        speaker="角色",
                        source=f"scene{scene}.ks",
                        engine="k",
                        index=len(rows) + 1,
                    )
                )
        target = self._tmp / "scenes.pdf"
        result = scriptpdf._build(
            rows, target, scriptpdf.ScriptPdfOptions(scene_per_page=True),
            None,
            FakeFont(),
            "FakeCJK", "fake.ttf",
        )
        self.assertGreaterEqual(result.pages, 4)
        self.assertEqual(result.scenes, 4)


class RealFontTests(unittest.TestCase):
    """用系统里的真实中文字体，验证子集化确实生效。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_realfont_"))
        try:
            cls.font, cls.font_name, cls.font_path = scriptpdf.pick_font(
                scriptpdf.ScriptPdfOptions()
            )
        except Exception as exc:
            cls.font = None
            cls.reason = str(exc)

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "font", None) is not None:
            try:
                cls.font.close()
            except Exception:
                pass
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self) -> None:
        if self.font is None:
            self.skipTest(f"没有可用的中文字体：{self.reason}")

    def test_real_pdf_is_small_and_valid(self) -> None:
        lines = sample_lines(400)
        target = self._tmp / "real.pdf"
        result = scriptpdf._build(
            lines,
            target,
            scriptpdf.ScriptPdfOptions(title="真实字体测试"),
            None,
            self.font,
            self.font_name,
            self.font_path,
        )
        probe = PdfProbe(target.read_bytes())
        self.assertGreaterEqual(probe.count, 5)
        self.assertEqual(result.missing_chars, [], f"不该缺字：{result.missing_chars}")
        # 子集化的意义：几百条台词不该让 PDF 背上十几 MB 的整字体
        self.assertLess(
            result.size_bytes,
            3 * 1024 * 1024,
            f"PDF 过大（{result.size_bytes} 字节），字形子集化可能没生效",
        )
        self.assertGreater(result.size_bytes, 2000)

    def test_font_size_scales_with_charset(self) -> None:
        """用字越少，嵌入的字体越小 —— 这是子集化生效的直接证据。"""
        small = [TextLine(text="你好。", speaker="甲", source="a.ks", engine="k", index=1)]
        big = sample_lines(1200)
        sizes = []
        for label, rows in (("small", small), ("big", big)):
            target = self._tmp / f"size_{label}.pdf"
            result = scriptpdf._build(
                rows, target, scriptpdf.ScriptPdfOptions(), None,
                self.font, self.font_name, self.font_path,
            )
            sizes.append(result.size_bytes)
        self.assertLess(sizes[0], sizes[1])

    def test_subset_font_reparses(self) -> None:
        """从 PDF 里把嵌入的子集字体抠出来，确认它是一份能被重新解析的字体。"""
        from galtext import fontkit, pdfgen as pg

        lines = [TextLine(text="你好，世界。", speaker="甲", source="a.ks", engine="k", index=1)]
        chars = {ch for ln in lines for ch in ln.text + ln.speaker} | set("测试0123456789—／ ")
        document = pg.PdfDocument(self.font, chars, title="子集测试")
        page = document.new_page()
        page.text(72, 700, "你好，世界。", 12)
        target = self._tmp / "subset.pdf"
        document.save(target)

        probe = PdfProbe(target.read_bytes())
        font_streams = [
            body for dictionary, body in probe.streams() if b"/Length1" in dictionary
        ]
        self.assertEqual(len(font_streams), 1, "应该正好嵌入一份字体")
        subset_bytes = font_streams[0]
        self.assertGreater(len(subset_bytes), 1000)

        subset_path = self._tmp / "subset.ttf"
        subset_path.write_bytes(subset_bytes)
        reparsed = fontkit.load_font(str(subset_path))
        try:
            self.assertGreater(reparsed.num_glyphs, 100)
            for ch in "你好，世界。":
                self.assertEqual(
                    reparsed.glyph_id(ch),
                    self.font.glyph_id(ch),
                    f"子集字体里 {ch!r} 的字形 ID 变了",
                )
        finally:
            reparsed.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

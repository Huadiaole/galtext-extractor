"""textkit 的单元测试：编码探测 / 标签清洗 / 台词抽取 / 二进制扫描。"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from galtext import textkit as tk  # noqa: E402

JP = "これは日本語のテストです。ひらがなとカタカナが混ざっています。"
CN = "中文测试文本，用来验证编码探测是否正常工作。"
TW = "繁體中文測試文字，驗證編碼偵測是否正常。"
KR = "이것은 한국어 테스트 문장입니다."


class DecodeTests(unittest.TestCase):
    def _roundtrip(self, s: str, codec: str, expect: str | None = None) -> None:
        text, enc, _conf = tk.decode_auto(s.encode(codec))
        self.assertEqual(text, s, f"codec={codec} got enc={enc}")
        if expect:
            self.assertEqual(enc, expect, f"codec={codec}")

    def test_ascii(self) -> None:
        text, enc, conf = tk.decode_auto(b"This is a plain ASCII readme.")
        self.assertEqual(text, "This is a plain ASCII readme.")
        self.assertEqual(enc, "utf-8")
        self.assertGreater(conf, 0.9)

    def test_utf8_japanese(self) -> None:
        self._roundtrip(JP, "utf-8", "utf-8")

    def test_utf8_chinese(self) -> None:
        self._roundtrip(CN, "utf-8", "utf-8")

    def test_shift_jis(self) -> None:
        self._roundtrip(JP * 2, "cp932", "cp932")

    def test_shift_jis_short(self) -> None:
        self._roundtrip("こんにちは、先輩！", "cp932")

    def test_shift_jis_mixed_ascii(self) -> None:
        s = "「ようこそ、喫茶店へ」\nSelect: Yes / No\n"
        self._roundtrip(s, "cp932")

    def test_gbk(self) -> None:
        self._roundtrip(CN * 2, "cp936", "cp936")

    def test_big5(self) -> None:
        self._roundtrip(TW * 2, "cp950", "cp950")

    def test_korean_requires_explicit_encoding(self) -> None:
        # CP949 与 GBK 字节空间重叠，自动探测不区分韩文（见 textkit 注释），
        # 但显式指定编码时必须正确。
        text, enc, _ = tk.decode_with((KR * 2).encode("cp949"), "cp949")
        self.assertEqual(text, KR * 2)
        self.assertEqual(enc, "cp949")

    def test_utf16_with_bom(self) -> None:
        data = b"\xff\xfe" + JP.encode("utf-16-le")
        text, enc, _ = tk.decode_auto(data)
        self.assertEqual(text, JP)
        self.assertEqual(enc, "utf-16-le")

    def test_utf16_without_bom(self) -> None:
        # 无 BOM 的 UTF-16 文本必须能被认出来，否则 RealLive 之类会全乱
        data = (JP * 3).encode("utf-16-le")
        text, enc, _ = tk.decode_auto(data)
        self.assertEqual(text, JP * 3)
        self.assertEqual(enc, "utf-16-le")

    def test_shift_jis_not_mistaken_for_utf16(self) -> None:
        data = (JP * 3).encode("cp932")
        _text, enc, _ = tk.decode_auto(data)
        self.assertEqual(enc, "cp932")

    def test_has_japanese(self) -> None:
        self.assertTrue(tk.has_japanese(JP))
        self.assertFalse(tk.has_japanese("hello world"))
        self.assertTrue(tk.has_japanese(CN))
        self.assertTrue(tk.has_kana("カタカナ"))

    def test_looks_like_text(self) -> None:
        self.assertTrue(tk.looks_like_text((JP * 4).encode("cp932")))
        self.assertFalse(tk.looks_like_text(bytes(range(256)) * 8))


class CleanTests(unittest.TestCase):
    def test_kag_tags_stripped(self) -> None:
        line = "[wait time=500]「そんなの知らないよ」[r]"
        rows = tk.extract_dialogue(line)
        self.assertEqual(rows, [("", "そんなの知らないよ")])

    def test_ruby_stripped(self) -> None:
        line = "「[ruby text=かんじ]漢字[/ruby]だよ」"
        rows = tk.extract_dialogue(line)
        self.assertEqual([r[1] for r in rows], ["漢字だよ"])

    def test_speaker_quote(self) -> None:
        rows = tk.extract_dialogue("亜里沙「おはよう、先輩！」")
        self.assertEqual(rows, [("亜里沙", "おはよう、先輩！")])

    def test_speaker_colon(self) -> None:
        rows = tk.extract_dialogue("主人公：今日もいい天気だね。")
        self.assertEqual(rows, [("主人公", "今日もいい天気だね。")])

    def test_multiple_quotes_in_one_line(self) -> None:
        rows = tk.extract_dialogue("「行くぞ」「おう！」")
        self.assertEqual([r[1] for r in rows], ["行くぞ", "おう！"])

    def test_comment_rejected(self) -> None:
        self.assertEqual(tk.extract_dialogue("; コメント行"), [])
        self.assertEqual(tk.extract_dialogue("// comment"), [])
        self.assertEqual(tk.extract_dialogue("#include <stdio.h>"), [])

    def test_code_rejected(self) -> None:
        self.assertEqual(tk.extract_dialogue("    return 0;"), [])
        self.assertEqual(tk.extract_dialogue("for (int i = 0; i < 10; i++) {"), [])

    def test_string_literal_extracted_from_code(self) -> None:
        rows = tk.extract_dialogue('sf.text = "これはテストです"')
        self.assertEqual([r[1] for r in rows], ["これはテストです"])

    def test_ascii_only_dropped(self) -> None:
        # 默认要求含日文，纯 ASCII 行被丢弃
        self.assertEqual(tk.extract_dialogue("Hello, world!"), [])
        # 关掉「必须含日文」后才会收进来
        opts = tk.CleanOptions(require_japanese=False, drop_ascii_only=False)
        self.assertEqual(tk.extract_dialogue("Hello, world!", opts), [("", "Hello, world!")])

    def test_min_len(self) -> None:
        self.assertEqual(tk.extract_dialogue("あ", tk.CleanOptions(min_len=3)), [])
        self.assertEqual([r[1] for r in tk.extract_dialogue("あいう")], ["あいう"])

    def test_normalize(self) -> None:
        self.assertEqual(tk.normalize_text("  あ　い  \t う "), "あ い う")

    def test_iter_dialogue_lines(self) -> None:
        script = "こんにちは\n; skip\n「やあ」\n"
        got = list(tk.iter_dialogue_lines(script))
        self.assertEqual([g[2] for g in got], ["こんにちは", "やあ"])
        self.assertEqual([g[0] for g in got], [1, 3])

    def test_dedupe(self) -> None:
        rows = [("", "あ"), ("", "あ"), ("", "い")]
        self.assertEqual(tk.dedupe_pairs(rows), [("", "あ"), ("", "い")])


class ChineseOnlyTests(unittest.TestCase):
    """「只保留中文行」：按假名一刀切，把日文原文滤掉。"""

    def test_japanese_dropped(self) -> None:
        opts = tk.CleanOptions(chinese_only=True)
        self.assertEqual(tk.extract_dialogue("おはよう、先輩。", opts), [])
        self.assertEqual(tk.extract_dialogue("悠斗「今日もいい天気だね」", opts), [])

    def test_chinese_kept(self) -> None:
        opts = tk.CleanOptions(chinese_only=True)
        self.assertEqual(
            [r[1] for r in tk.extract_dialogue("早上好，前辈。", opts)],
            ["早上好，前辈。"],
        )
        self.assertEqual(
            [r[1] for r in tk.extract_dialogue("悠斗「今天天气也不错呢」", opts)],
            ["今天天气也不错呢"],
        )

    def test_chinese_with_leftover_kana_is_dropped(self) -> None:
        """混了假名的中文行也会被丢掉 —— 这是启发式的已知代价，写死在测试里。"""
        opts = tk.CleanOptions(chinese_only=True)
        self.assertEqual(tk.extract_dialogue("早上好，カラオケ去吧", opts), [])

    def test_off_by_default(self) -> None:
        self.assertEqual(
            [r[1] for r in tk.extract_dialogue("おはよう、先輩。")],
            ["おはよう、先輩。"],
        )
        self.assertFalse(tk.CleanOptions().chinese_only)

    def test_clone_carries_the_flag(self) -> None:
        opts = tk.CleanOptions().clone(chinese_only=True)
        self.assertTrue(opts.chinese_only)
        self.assertEqual(opts.min_len, 2)


class BinaryScanTests(unittest.TestCase):
    def test_sjis_run(self) -> None:
        blob = b"\x00\x01\x02" + "こんにちは元気ですか".encode("cp932") + b"\xff\xfe\x00"
        found = tk.scan_binary_strings(blob)
        self.assertTrue(any("こんにちは元気ですか" in f for f in found), found)

    def test_utf16_run(self) -> None:
        blob = b"\xde\xad\xbe\xef" + "今日はいい天気ですね".encode("utf-16-le") + b"\x00\x01\x02\x03"
        found = tk.scan_binary_strings(blob)
        self.assertTrue(any("今日はいい天気ですね" in f for f in found), found)

    def test_no_false_positive_on_random(self) -> None:
        import random

        rng = random.Random(1234)
        blob = bytes(rng.randrange(256) for _ in range(4096))
        found = tk.scan_binary_strings(blob)
        # 随机数据里不该出现成片的日文
        self.assertLess(len(found), 8, found[:5])


if __name__ == "__main__":
    unittest.main(verbosity=2)

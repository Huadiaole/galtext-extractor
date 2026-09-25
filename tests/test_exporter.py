"""导出器测试：每种格式都要能写出来并且能读回去。"""

from __future__ import annotations

import csv
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from galtext import exporter  # noqa: E402
from galtext.common import ScanResult, ScriptReport, TextLine  # noqa: E402

LINES = [
    TextLine(text="おはよう、先輩。", speaker="悠斗", source="a.ks", engine="kirikiri", index=1),
    TextLine(text="空は青く澄み渡っていた。", speaker="", source="a.ks", engine="kirikiri", index=2),
    TextLine(text="「ようこそ、喫茶店へ」", speaker="謎の少女", source="b.ks", engine="kirikiri", index=3),
]


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_export_"))
        self.out = self._tmp / "out"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_txt_with_speaker(self) -> None:
        path = exporter.export(self.out.with_suffix(".txt"), LINES, fmt="txt")
        body = path.read_text(encoding="utf-8-sig")
        self.assertIn("悠斗：おはよう、先輩。", body)
        self.assertIn("空は青く澄み渡っていた。", body)
        self.assertEqual(len(body.strip().splitlines()), 3)

    def test_txt_without_speaker(self) -> None:
        path = exporter.export(
            self.out.with_suffix(".txt"), LINES, fmt="txt", with_speaker=False
        )
        body = path.read_text(encoding="utf-8-sig")
        self.assertIn("おはよう、先輩。", body)
        self.assertNotIn("悠斗：", body)

    def test_csv_roundtrip(self) -> None:
        path = exporter.export(self.out.with_suffix(".csv"), LINES, fmt="csv")
        with path.open(encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp))
        self.assertEqual(rows[0], ["序号", "说话人", "文本", "来源", "引擎"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1][1], "悠斗")
        self.assertEqual(rows[1][2], "おはよう、先輩。")

    def test_tsv_roundtrip(self) -> None:
        path = exporter.export(self.out.with_suffix(".tsv"), LINES, fmt="tsv")
        with path.open(encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp, delimiter="\t"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2][2], "空は青く澄み渡っていた。")

    def test_template_has_empty_translation_column(self) -> None:
        path = exporter.export(self.out.with_suffix(".csv"), LINES, fmt="template")
        with path.open(encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp))
        self.assertEqual(rows[0], ["序号", "说话人", "原文", "译文", "来源", "引擎"])
        for row in rows[1:]:
            self.assertEqual(row[3], "", "译文列必须留空")
            self.assertTrue(row[2], "原文列不能空")

    def test_json_includes_result_metadata(self) -> None:
        result = ScanResult(
            root="D:/Game",
            engines=[("kirikiri", 100)],
            scripts=[ScriptReport(virtual_path="a.ks", engine="kirikiri", line_count=3)],
            lines=LINES,
        )
        path = exporter.export(self.out.with_suffix(".json"), LINES, fmt="json", result=result)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(payload["root"], "D:/Game")
        self.assertEqual(len(payload["lines"]), 3)
        self.assertEqual(payload["lines"][0]["speaker"], "悠斗")
        self.assertEqual(payload["engines"][0]["id"], "kirikiri")

    def test_jsonl(self) -> None:
        path = exporter.export(self.out.with_suffix(".jsonl"), LINES, fmt="jsonl")
        rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8-sig").splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["text"], "「ようこそ、喫茶店へ」")

    def test_cp932_output(self) -> None:
        path = exporter.export(self.out.with_suffix(".txt"), LINES, fmt="txt", encoding="cp932")
        raw = path.read_bytes()
        self.assertEqual(raw.decode("cp932").strip().splitlines()[0], "悠斗：おはよう、先輩。")

    def test_utf8_sig_has_bom(self) -> None:
        path = exporter.export(self.out.with_suffix(".txt"), LINES, fmt="txt", encoding="utf-8-sig")
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_directory_target(self) -> None:
        path = exporter.export(self._tmp, LINES, fmt="csv")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "galtext.csv")

    def test_unknown_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            exporter.export(self.out, LINES, fmt="docx")

    def test_empty_lines_produce_empty_file(self) -> None:
        path = exporter.export(self.out.with_suffix(".txt"), [], fmt="txt")
        self.assertEqual(path.read_text(encoding="utf-8-sig"), "")

    def test_all_declared_formats_work(self) -> None:
        for fmt in exporter.FORMATS:
            with self.subTest(fmt=fmt):
                path = exporter.export(
                    self._tmp / f"f_{fmt}{exporter.EXTENSIONS[fmt]}", LINES, fmt=fmt
                )
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

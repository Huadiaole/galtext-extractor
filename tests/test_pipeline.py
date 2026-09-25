"""调度层测试：用合成游戏目录跑完整的 scan 流程。"""

from __future__ import annotations

import pathlib
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from galtext import parsers, pipeline, textkit  # noqa: E402
from tests import fixtures  # noqa: E402


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_test_"))
        cls.game = fixtures.make_mixed_game(cls._tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_engines_detected(self) -> None:
        result = pipeline.scan(self.game)
        ids = [key for key, _score in result.engines]
        self.assertIn("kirikiri", ids)
        self.assertIn("nscripter", ids)
        # 置信度必须降序
        scores = [score for _key, score in result.engines]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_extracts_expected_phrases(self) -> None:
        result = pipeline.scan(self.game)
        texts = {line.text for line in result.lines}
        for phrase in fixtures.expected_japanese_phrases():
            self.assertIn(phrase, texts, f"缺少台词：{phrase}")

    def test_speakers_detected(self) -> None:
        result = pipeline.scan(self.game)
        speakers = {line.speaker for line in result.lines if line.speaker}
        self.assertIn("悠斗", speakers)
        self.assertIn("先輩", speakers)
        self.assertIn("謎の少女", speakers)
        self.assertIn("散装脚本", speakers)

    def test_tags_and_comments_dropped(self) -> None:
        result = pipeline.scan(self.game)
        blob = "\n".join(line.text for line in result.lines)
        self.assertNotIn("wait time", blob)
        self.assertNotIn("コメント行", blob)
        self.assertNotIn("f.flag", blob)
        self.assertNotIn("bg01.jpg", blob)

    def test_archive_members_are_read(self) -> None:
        result = pipeline.scan(self.game)
        sources = {script.virtual_path for script in result.scripts}
        self.assertIn("data.xp3/scenario/prologue.ks", sources)
        self.assertIn("data.xp3/scenario/chapter1.ks", sources)
        self.assertIn("scenario/loose.ks", sources)
        self.assertIn("nscript.dat", sources)

    def test_dedupe_flag_matters(self) -> None:
        with_dedupe = pipeline.scan(self.game, pipeline.ExtractOptions(dedupe=True))
        without = pipeline.scan(self.game, pipeline.ExtractOptions(dedupe=False))
        self.assertLessEqual(len(with_dedupe.lines), len(without.lines))

    def test_engine_restriction(self) -> None:
        result = pipeline.scan(self.game, pipeline.ExtractOptions(engines=["nscripter"]))
        self.assertTrue(all(s.engine == "nscripter" for s in result.scripts))
        texts = {line.text for line in result.lines}
        self.assertIn("選択してください", texts)
        # KiriKiri 独有的台词不该出现
        self.assertNotIn("ようこそ、喫茶店へ", texts)

    def test_cleaning_options(self) -> None:
        result = pipeline.scan(
            self.game,
            pipeline.ExtractOptions(clean=textkit.CleanOptions(min_len=12)),
        )
        self.assertTrue(result.lines)
        for line in result.lines:
            self.assertGreaterEqual(len(line.text), 12)

    def test_missing_directory(self) -> None:
        result = pipeline.scan(self.game / "does-not-exist")
        self.assertEqual(result.lines, [])
        self.assertTrue(result.warnings)

    def test_cancel(self) -> None:
        result = pipeline.scan(self.game, should_cancel=lambda: True)
        self.assertTrue(result.cancelled)

    def test_progress_reported(self) -> None:
        seen: list[str] = []
        pipeline.scan(self.game, progress=lambda _p, msg: seen.append(msg))
        self.assertTrue(seen)
        self.assertTrue(any("完成" in msg for msg in seen))

    def test_scripts_report_has_line_counts(self) -> None:
        result = pipeline.scan(self.game)
        self.assertTrue(result.scripts)
        self.assertTrue(any(s.line_count > 0 for s in result.scripts))
        self.assertTrue(all(s.encoding for s in result.scripts))

    def test_format_summary(self) -> None:
        result = pipeline.scan(self.game)
        summary = pipeline.format_summary(result)
        self.assertIn("KiriKiri", summary)
        self.assertIn("文本", summary)


class GenericFallbackTests(unittest.TestCase):
    """没有已知引擎特征时，通用二进制扫描要能捞到文本。"""

    def setUp(self) -> None:
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_generic_"))
        rng = random.Random(99)
        phrase = "これはテスト用の埋め込みテキストです"
        blob = (
            bytes(rng.randrange(256) for _ in range(512))
            + phrase.encode("cp932")
            + bytes(rng.randrange(256) for _ in range(512))
        )
        self.game = self._tmp / "Mystery"
        self.game.mkdir()
        (self.game / "mystery.bin").write_bytes(blob)
        self.phrase = phrase

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_auto_generic_when_nothing_else(self) -> None:
        result = pipeline.scan(self.game)
        texts = {line.text for line in result.lines}
        # 兜底扫描是按字节流切的，串首/串尾可能粘上一个相邻的乱码字，
        # 所以这里用「包含」而不是「相等」来判定。
        self.assertTrue(
            any(self.phrase in text for text in texts),
            f"通用扫描没找到埋入的文本：{sorted(texts)}",
        )

    def test_explicit_generic(self) -> None:
        result = pipeline.scan(self.game, pipeline.ExtractOptions(include_generic=True))
        texts = {line.text for line in result.lines}
        self.assertTrue(
            any(self.phrase in text for text in texts),
            f"通用扫描没找到埋入的文本：{sorted(texts)}",
        )


class PatchOverrideTests(unittest.TestCase):
    """「原版 + 汉化补丁并存」时的封包覆盖语义。

    这是汉化 galgame 最常见的形态：``data.xp3`` 是日文原版，``patch.xp3`` 是汉化补丁，
    两者装着**同名**脚本。引擎按文件名顺序加载、后者覆盖前者，
    所以工具也只该保留胜出的那一份 —— 否则原文会和译文一起被提出来。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = pathlib.Path(tempfile.mkdtemp(prefix="galtext_patch_"))
        cls.game = fixtures.make_patched_game(cls._tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self) -> None:
        if self.game is None:
            self.skipTest("kirikiri 模块不可用，无法构造补丁样本")

    def _texts(self, result) -> str:
        return "\n".join(line.text for line in result.lines)

    def test_patch_shadows_base(self) -> None:
        result = pipeline.scan(self.game)
        blob = self._texts(result)
        for phrase in fixtures.PATCH_CN_PHRASES:
            self.assertIn(phrase, blob, f"汉化正文缺失：{phrase}")
        for phrase in fixtures.PATCH_JP_PHRASES:
            self.assertNotIn(phrase, blob, f"原版正文不该出现：{phrase}")

    def test_loose_file_beats_archive(self) -> None:
        result = pipeline.scan(self.game)
        blob = self._texts(result)
        self.assertIn(fixtures.PATCH_CN_LOOSE_TEXT, blob)
        self.assertNotIn(fixtures.PATCH_JP_LOOSE_TEXT, blob)

    def test_overridden_scripts_are_reported(self) -> None:
        result = pipeline.scan(self.game)
        losers = [s for s in result.scripts if s.note]
        self.assertTrue(losers, "被覆盖的脚本应当出现在报告里，而不是悄悄消失")

        paths = {s.virtual_path for s in losers}
        # 日文原版被补丁盖掉；散装文件又盖掉了两个封包里的同名脚本
        self.assertIn("data.xp3/scenario/prologue.ks", paths)
        self.assertIn("data.xp3/scenario/loose.ks", paths)
        self.assertIn("patch.xp3/scenario/loose.ks", paths)

        for script in losers:
            self.assertEqual(script.line_count, 0)
            self.assertIn("覆盖", script.note)
        # note 里写的是**胜出者**，所以补丁名应该出现在其中一条上
        self.assertTrue(any("patch.xp3" in s.note for s in losers), [s.note for s in losers])
        self.assertTrue(any("覆盖" in w for w in result.warnings))

    def test_disabling_override_keeps_both_versions(self) -> None:
        result = pipeline.scan(self.game, pipeline.ExtractOptions(override_archives=False))
        blob = self._texts(result)
        self.assertIn(fixtures.PATCH_CN_PHRASES[0], blob)
        self.assertIn(fixtures.PATCH_JP_PHRASES[0], blob)
        self.assertFalse([s.note for s in result.scripts if s.note])

    def test_chinese_only_filters_out_japanese(self) -> None:
        """两版都在的情况下，「只保留中文行」应当只留下译文。"""
        result = pipeline.scan(
            self.game,
            pipeline.ExtractOptions(
                override_archives=False,
                clean=textkit.CleanOptions(chinese_only=True),
            ),
        )
        blob = self._texts(result)
        for phrase in fixtures.PATCH_CN_PHRASES:
            self.assertIn(phrase, blob)
        for phrase in fixtures.PATCH_JP_PHRASES:
            self.assertNotIn(phrase, blob)

    def test_split_virtual_path(self) -> None:
        self.assertEqual(
            pipeline.split_virtual_path("data.xp3/scenario/a.ks"),
            ("data.xp3", "scenario/a.ks"),
        )
        self.assertEqual(
            pipeline.split_virtual_path("scenario/a.ks"), ("", "scenario/a.ks")
        )
        # nscript.dat 是散装脚本，不能被误判成封包
        self.assertEqual(pipeline.split_virtual_path("nscript.dat"), ("", "nscript.dat"))
        self.assertEqual(
            pipeline.split_virtual_path("arc.nsa/nscript.dat"),
            ("arc.nsa", "nscript.dat"),
        )


class RegistryTests(unittest.TestCase):
    def test_registry_loads_without_crashing(self) -> None:
        infos = parsers.list_engines()
        keys = {info["key"] for info in infos}
        for expected in ("kirikiri", "nscripter", "plaintext", "generic"):
            self.assertIn(expected, keys)
        self.assertTrue(any(info["available"] for info in infos))

    def test_broken_module_is_isolated(self) -> None:
        # 即使某个引擎模块缺失/报错，也必须能列出其它引擎
        for info in parsers.list_engines():
            self.assertIsInstance(info["available"], bool)
            if not info["available"]:
                self.assertTrue(info["error"])

    def test_discovery_covers_modules_on_disk(self) -> None:
        """自动发现必须覆盖 parsers/ 下的每一个模块 —— 不依赖写死的清单。"""
        import pkgutil

        on_disk = {
            info.name
            for info in pkgutil.iter_modules(parsers.__path__)
            if not info.name.startswith("_") and info.name != "__init__"
        }
        self.assertTrue(on_disk, "parsers/ 下应该有模块")
        discovered = set(parsers.discover_modules())
        self.assertTrue(on_disk <= discovered, f"漏掉的模块：{on_disk - discovered}")
        # 磁盘上没有的条目只允许是「计划中」的引擎
        self.assertTrue(
            (discovered - on_disk) <= set(parsers.PLANNED_ENGINES),
            f"多出来的模块：{discovered - on_disk - set(parsers.PLANNED_ENGINES)}",
        )

    def test_builtin_engines_are_ordered_first(self) -> None:
        import pkgutil

        on_disk = {
            info.name
            for info in pkgutil.iter_modules(parsers.__path__)
            if not info.name.startswith("_") and info.name != "__init__"
        }
        order = parsers.discover_modules()
        builtin = [n for n in parsers.PREFERRED_ORDER if n in on_disk and n in order]
        self.assertEqual(order[: len(builtin)], builtin, "内置引擎应当排在最前面且保持既定顺序")

    def test_planned_engines_stay_visible_as_unavailable(self) -> None:
        """计划中但未实现的引擎不能悄悄消失 —— 要如实显示成「不可用」。"""
        import pkgutil

        on_disk = {
            info.name
            for info in pkgutil.iter_modules(parsers.__path__)
            if not info.name.startswith("_") and info.name != "__init__"
        }
        listed = {info["key"]: info for info in parsers.list_engines()}
        for name in parsers.PLANNED_ENGINES:
            if name in on_disk:
                continue  # 已经实现了，跳过
            with self.subTest(engine=name):
                self.assertIn(name, parsers.discover_modules())
                self.assertIn(name, listed)
                self.assertFalse(listed[name]["available"])
                self.assertTrue(listed[name]["error"])

    def test_registry_covers_discovered_modules(self) -> None:
        registry = parsers.registry()
        for name in parsers.discover_modules():
            with self.subTest(module=name):
                self.assertIn(name, registry)

    def test_dropping_a_module_in_is_enough(self) -> None:
        """README 承诺「把 .py 丢进 parsers/ 就能用」—— 这里真的试一次。

        会临时写入一个探针模块，结束时删掉。清理顺序是 LIFO，
        所以先登记「重新加载注册表」再登记「删文件」：
        这样删文件先执行、注册表后重载，缓存里不会留下探针。
        """
        probe = pathlib.Path(parsers.__file__).parent / "zz_probe_engine.py"
        self.assertTrue(probe.parent.is_dir())
        probe.write_text(
            '"""测试用探针解析器，跑完就删。"""\n'
            'ENGINE_ID = "probe"\n'
            'ENGINE_NAME = "Probe Engine"\n'
            "def detect_dir(root):\n"
            "    return 0\n"
            "def iter_scripts(root):\n"
            "    return iter(())\n"
            "def extract_lines(vpath, data):\n"
            "    return []\n",
            encoding="utf-8",
        )
        self.addCleanup(parsers.registry, True)
        self.addCleanup(lambda: probe.unlink(missing_ok=True))
        try:
            self.assertIn("zz_probe_engine", parsers.discover_modules())

            registry = parsers.registry(reload=True)
            self.assertIn("zz_probe_engine", registry)
            info = registry["zz_probe_engine"]
            self.assertTrue(info.ok, f"探针模块加载失败：{info.error}")
            self.assertEqual(parsers.engine_name("zz_probe_engine"), "Probe Engine")
        finally:
            # 让后续用例看到干净的注册表，不然会带上这个探针
            probe.unlink(missing_ok=True)
            parsers.registry(reload=True)

    def test_extension_routing(self) -> None:
        kirikiri = parsers.get("kirikiri")
        if kirikiri is None:
            self.skipTest("kirikiri 模块不可用")
        self.assertIs(parsers.pick_extractor("plaintext", "scenario/a.ks"), kirikiri)
        self.assertIs(parsers.pick_extractor("plaintext", "readme.txt"), parsers.get("plaintext"))

    def test_detect_returns_empty_for_non_game_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scores = dict(parsers.detect(pathlib.Path(tmp)))
            # 空目录不该被任何专业解析器认领
            for engine in ("kirikiri", "nscripter", "reallive", "bgi"):
                self.assertLess(scores.get(engine, 0), 50, engine)


if __name__ == "__main__":
    unittest.main(verbosity=2)

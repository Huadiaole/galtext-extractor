"""主题与界面资源的测试。

需要图形环境的用例在无显示的机器（比如 Linux CI）上会自动 **skip** ——
测试套件的其余部分从不创建 Tk 窗口，所以 CI 不需要 xvfb。
"""

from __future__ import annotations

import pathlib
import re
import sys
import tkinter as tk
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from galtext import theme as theme_mod  # noqa: E402

HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 界面用到的工具栏图标，一个都不能少
TOOLBAR_ICONS = (
    "folder",
    "scan",
    "stop",
    "export",
    "copy",
    "search",
    "sun",
    "moon",
    "pdf",
    "font",
    "info",
    "grid",
)

ICON_VARIANTS = ("light", "dark", "accent")
APP_ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def _require_tk() -> tk.Tk:
    """拿一个可用（且隐藏的）Tk 根窗口；没有图形环境就跳过用例。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 取决于 CI 环境
        raise unittest.SkipTest(f"没有可用的图形环境：{exc}") from exc
    root.withdraw()
    return root


class PaletteTests(unittest.TestCase):
    """调色板是纯数据，任何环境都能测。"""

    def test_both_modes_exist(self) -> None:
        self.assertIn("light", theme_mod.PALETTES)
        self.assertIn("dark", theme_mod.PALETTES)

    def test_modes_have_identical_keys(self) -> None:
        """明暗两套必须一一对应，否则切主题时必然 KeyError。"""
        light = set(theme_mod.PALETTES["light"])
        dark = set(theme_mod.PALETTES["dark"])
        self.assertEqual(light, dark, f"缺少的键：{light ^ dark}")
        self.assertGreater(len(light), 20)

    def test_every_value_is_a_hex_colour(self) -> None:
        for mode, palette in theme_mod.PALETTES.items():
            for key, value in palette.items():
                with self.subTest(mode=mode, key=key):
                    self.assertRegex(value, HEX_COLOUR)

    def test_mode_labels_cover_modes(self) -> None:
        self.assertEqual(set(theme_mod.MODE_LABELS), set(theme_mod.PALETTES))

    def test_dark_is_actually_darker(self) -> None:
        def luminance(mode: str) -> float:
            value = theme_mod.PALETTES[mode]["bg"].lstrip("#")
            r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        self.assertLess(luminance("dark"), luminance("light") - 100)

    def test_describe_theme_returns_text(self) -> None:
        text = theme_mod.describe_theme()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())


class AssetTests(unittest.TestCase):
    """图标是构建期产物，缺失会让界面缺图但不该让程序崩。"""

    def test_app_icons_exist(self) -> None:
        for size in APP_ICON_SIZES:
            with self.subTest(size=size):
                self.assertTrue(
                    (theme_mod.ASSETS_DIR / f"app_{size}.png").exists(),
                    f"缺少 app_{size}.png（跑 python scripts/make_assets.py 重新生成）",
                )

    def test_ico_exists(self) -> None:
        self.assertTrue((theme_mod.ASSETS_DIR / "app.ico").exists())

    def test_toolbar_icons_exist_in_all_variants(self) -> None:
        for variant in ICON_VARIANTS:
            for name in TOOLBAR_ICONS:
                with self.subTest(variant=variant, icon=name):
                    self.assertTrue(
                        (theme_mod.ASSETS_DIR / "icons" / variant / f"{name}.png").exists()
                    )

    def test_png_dimensions(self) -> None:
        """尺寸不对会让界面错位，所以这里连像素一起校验。"""
        try:
            from PIL import Image
        except Exception:  # pragma: no cover - Pillow 是开发依赖
            self.skipTest("没有安装 Pillow，跳过像素级校验")

        for size in APP_ICON_SIZES:
            with self.subTest(size=size):
                with Image.open(theme_mod.ASSETS_DIR / f"app_{size}.png") as img:
                    self.assertEqual(img.size, (size, size))
                    self.assertEqual(img.mode, "RGBA")

        for variant in ICON_VARIANTS:
            for name in TOOLBAR_ICONS:
                with self.subTest(variant=variant, icon=name):
                    with Image.open(
                        theme_mod.ASSETS_DIR / "icons" / variant / f"{name}.png"
                    ) as img:
                        self.assertEqual(img.size, (20, 20))
                        self.assertEqual(img.mode, "RGBA")

    def test_line_icons_are_not_solid_blobs(self) -> None:
        """线条图标不该是一整块实心色 —— 那说明画成了填充而不是描边。"""
        try:
            from PIL import Image
        except Exception:  # pragma: no cover
            self.skipTest("没有安装 Pillow")

        for name in TOOLBAR_ICONS:
            path = theme_mod.ASSETS_DIR / "icons" / "light" / f"{name}.png"
            with Image.open(path) as img:
                alpha = img.convert("RGBA").getchannel("A")
                pixels = list(alpha.getdata())
            ink = sum(1 for a in pixels if a > 40) / len(pixels)
            with self.subTest(icon=name):
                self.assertGreater(ink, 0.02, f"{name} 几乎没有内容")
                self.assertLess(ink, 0.55, f"{name} 看起来是实心填充而不是线条")


class ThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _require_tk()
        self.addCleanup(self.root.destroy)

    def test_applies_in_both_modes(self) -> None:
        light = theme_mod.Theme(self.root, mode="light")
        light_bg = light.style.lookup("Gal.Header.TFrame", "background")
        self.assertTrue(light_bg)

        light.set_mode("dark")
        dark_bg = light.style.lookup("Gal.Header.TFrame", "background")
        self.assertNotEqual(light_bg, dark_bg, "切换模式后表头背景应该变化")
        self.assertEqual(dark_bg.lower(), theme_mod.PALETTES["dark"]["surface"].lower())

    def test_unknown_mode_falls_back_to_light(self) -> None:
        theme = theme_mod.Theme(self.root, mode="nonsense")
        self.assertEqual(theme.mode, "light")

    def test_toggle_round_trip(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        self.assertEqual(theme.toggle_mode(), "dark")
        self.assertEqual(theme.toggle_mode(), "light")

    def test_set_mode_is_idempotent(self) -> None:
        theme = theme_mod.Theme(self.root, mode="dark")
        theme.set_mode("dark")  # 不该抛异常
        self.assertEqual(theme.mode, "dark")

    def test_custom_styles_are_registered(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        for name in (
            "Gal.Accent.TButton",
            "Gal.Ghost.TButton",
            "Gal.Tool.TButton",
            "Gal.Header.TFrame",
            "Gal.Title.TLabel",
            "Gal.Muted.TLabel",
            "Gal.Horizontal.TProgressbar",
        ):
            with self.subTest(style=name):
                self.assertTrue(
                    theme.style.lookup(name, "background") is not None,
                    f"样式 {name} 没有注册",
                )

    def test_icons_load_and_change_with_mode(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        for name in ("scan", "folder", "export", "pdf"):
            with self.subTest(icon=name):
                self.assertIsNotNone(theme.icon(name), f"{name} 在浅色模式下加载失败")
        light_scan = theme.icon("scan")
        theme.set_mode("dark")
        dark_scan = theme.icon("scan")
        self.assertIsNotNone(dark_scan, "scan 在深色模式下加载失败")
        # 两套描边色是不同文件，缓存键不同，对象也应当不同
        self.assertIsNot(light_scan, dark_scan)

    def test_icon_on_accent_exists(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        self.assertIsNotNone(theme.icon_on_accent("scan"))

    def test_missing_icon_returns_none_instead_of_raising(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        self.assertIsNone(theme.icon("definitely-not-an-icon"))

    def test_font_helper(self) -> None:
        theme = theme_mod.Theme(self.root, mode="light")
        font = theme.font(11, "bold")
        self.assertIsInstance(font, tuple)
        self.assertIn(11, font)
        self.assertIn("bold", font)


class AppSmokeTests(unittest.TestCase):
    """真的把主窗口建出来 —— 覆盖样式与图标挂载的集成路径。"""

    def setUp(self) -> None:
        self.root = _require_tk()
        self.addCleanup(self.root.destroy)

    def test_app_constructs_and_retints(self) -> None:
        from galtext import gui

        app = gui.GalTextApp(self.root, None)
        self.assertIsNotNone(app.theme)
        self.assertTrue(app.theme.mode in theme_mod.PALETTES)

        # 切到深色再切回来：不该抛异常，状态栏要有反馈
        app._set_mode("dark")
        self.assertEqual(app.theme.mode, "dark")
        self.assertIn("深色", app.status_var.get())

        app._set_mode("light")
        self.assertEqual(app.theme.mode, "light")
        self.assertIn("浅色", app.status_var.get())

    def test_window_title_has_version(self) -> None:
        from galtext import __version__, gui

        gui.GalTextApp(self.root, None)
        self.assertIn(__version__, self.root.title())

    def test_about_dialog_text_mentions_author(self) -> None:
        from galtext import __author__

        self.assertTrue(__author__)
        source = pathlib.Path(__file__).resolve().parents[1] / "galtext" / "gui.py"
        self.assertIn("__author__", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

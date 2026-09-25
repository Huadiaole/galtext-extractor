"""界面主题：配色、ttk 样式与图标。

设计取舍：

* **默认走自绘主题**。ttk 自带的 ``clam`` 是唯一能完全自定义颜色的内置主题，
  所以在它上面重新配色，而不是依赖第三方库 —— 这样「解压即用、零依赖」的
  卖点不会被破坏，任何人 clone 下来直接跑就有统一的观感。
* **装了 ``sv-ttk`` 就自动升级**成 Fluent / Windows 11 风格。
  这是可选增强：装了就更好看，没装也完全不影响使用（见 ``Theme.base``）。
* 颜色全部集中在 :data:`PALETTES` 里，明暗两套共用同一组语义键名，
  切换主题时只需要重新 ``apply()`` 一次。

图标是构建期由 ``scripts/make_assets.py`` 用 Pillow 生成的 PNG，
运行期只用标准库的 ``tk.PhotoImage`` 加载 —— 所以运行时不依赖 Pillow。
"""

from __future__ import annotations

import logging
import os
import pathlib
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

log = logging.getLogger(__name__)

ASSETS_DIR = pathlib.Path(__file__).resolve().parent / "assets"

#: 品牌色（自绘主题与图标共用同一组，保证视觉一致）
BRAND_ACCENT = "#4F6BED"
BRAND_ACCENT_HOVER = "#3D57D6"
BRAND_ACCENT_PRESSED = "#3248B8"
BRAND_ACCENT2 = "#8B5CF6"
BRAND_CYAN = "#22D3EE"

#: 两套调色板。键名是语义化的，明暗一一对应。
PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F5F6FA",
        "surface": "#FFFFFF",
        "surface_alt": "#EEF0F7",
        "surface_hover": "#E6E9F4",
        "border": "#E1E4EF",
        "border_strong": "#CDD2E2",
        "text": "#171A22",
        "muted": "#6B7280",
        "faint": "#9AA1B1",
        "accent": BRAND_ACCENT,
        "accent_hover": BRAND_ACCENT_HOVER,
        "accent_pressed": BRAND_ACCENT_PRESSED,
        "accent_text": "#FFFFFF",
        "accent_soft": "#E8ECFD",
        "accent2": BRAND_ACCENT2,
        "cyan": BRAND_CYAN,
        "success": "#16A34A",
        "warning": "#D97706",
        "danger": "#DC2626",
        "selection_bg": "#DAE1FB",
        "selection_fg": "#141A2E",
        "speaker": "#1F4FC4",
        "narration": "#6B7280",
        "code": "#8A5CF6",
        "highlight": "#FFE9A8",
    },
    "dark": {
        "bg": "#14161C",
        "surface": "#1C1F27",
        "surface_alt": "#232733",
        "surface_hover": "#2B3040",
        "border": "#2B2F3A",
        "border_strong": "#3A4050",
        "text": "#E9EBF2",
        "muted": "#98A0B0",
        "faint": "#6E7686",
        "accent": "#6D86F5",
        "accent_hover": "#8298F8",
        "accent_pressed": "#5A73E0",
        "accent_text": "#10131A",
        "accent_soft": "#252B3D",
        "accent2": "#A78BFA",
        "cyan": "#4FD8EE",
        "success": "#34D399",
        "warning": "#FBBF24",
        "danger": "#F87171",
        "selection_bg": "#33406B",
        "selection_fg": "#F2F4FA",
        "speaker": "#8FB0FF",
        "narration": "#98A0B0",
        "code": "#C4A6FF",
        "highlight": "#6B5A1E",
    },
}

MODE_LABELS = {"light": "浅色", "dark": "深色"}


def available_base_theme() -> str:
    """返回将要使用的基座主题名（``sv`` 或 ``clam``）。"""
    forced = os.environ.get("GALTEXT_THEME", "").strip().lower()
    if forced in ("native", "clam", "self"):
        return "clam"
    if forced == "sv":
        return "sv"
    try:
        import sv_ttk  # noqa: F401
    except Exception:
        return "clam"
    return "sv"


def pick_font_family(root: tk.Misc) -> str:
    """挑一个中英文都不难看的界面字体。"""
    try:
        families = set(tkfont.families(root))
    except Exception:  # pragma: no cover
        return "TkDefaultFont"
    for name in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Segoe UI",
        "Meiryo UI",
        "Noto Sans CJK SC",
        "PingFang SC",
        "Helvetica Neue",
    ):
        if name in families:
            return name
    return "TkDefaultFont"


class Theme:
    """把配色 + 样式 + 图标打包成一个对象，交给界面层使用。"""

    def __init__(self, root: tk.Misc, mode: str = "light") -> None:
        self.root = root
        self.style = ttk.Style(root)
        self.mode = mode if mode in PALETTES else "light"
        self.base = available_base_theme()
        self.family = pick_font_family(root)
        self._images: dict[str, tk.PhotoImage] = {}
        self._apply()

    # ------------------------------------------------------------------
    # 颜色与字体
    # ------------------------------------------------------------------
    @property
    def colors(self) -> dict[str, str]:
        return PALETTES[self.mode]

    def color(self, key: str) -> str:
        return self.colors.get(key, "#000000")

    def font(self, size: int = 10, weight: str = "normal", slant: str = "roman") -> tuple:
        if self.family == "TkDefaultFont":
            return ("TkDefaultFont", size, weight) if weight != "normal" else ("TkDefaultFont", size)
        return (self.family, size, weight, slant) if slant != "roman" else (self.family, size, weight)

    # ------------------------------------------------------------------
    # 图标
    # ------------------------------------------------------------------
    def image(self, relative: str) -> tk.PhotoImage | None:
        """按相对路径加载并缓存图标。Tk 不持有引用会被 GC，所以必须缓存。"""
        cached = self._images.get(relative)
        if cached is not None:
            return cached
        path = ASSETS_DIR / relative
        if not path.exists():
            return None
        try:
            image = tk.PhotoImage(master=self.root, file=str(path))
        except tk.TclError as exc:  # pragma: no cover - 资源缺失或格式不支持
            log.debug("图标加载失败 %s：%s", path, exc)
            return None
        self._images[relative] = image
        return image

    def icon(self, name: str) -> tk.PhotoImage | None:
        """工具栏图标，按当前明暗模式自动选描边颜色。"""
        variant = "light" if self.mode == "light" else "dark"
        return self.image(f"icons/{variant}/{name}.png")

    def icon_on_accent(self, name: str) -> tk.PhotoImage | None:
        """给强调色按钮用的白色描边图标。"""
        return self.image(f"icons/accent/{name}.png")

    def app_icon(self, size: int = 64) -> tk.PhotoImage | None:
        return self.image(f"app_{size}.png")

    def apply_window_icon(self, window: tk.Misc | None = None) -> None:
        """设置窗口/任务栏图标（PNG + ICO 双管齐下，覆盖不同平台）。"""
        target = window or self.root
        ico = ASSETS_DIR / "app.ico"
        if ico.exists() and os.name == "nt":
            try:
                target.iconbitmap(default=str(ico))  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass
        image = self.app_icon(64)
        if image is not None:
            try:
                target.iconphoto(True, image)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in PALETTES or mode == self.mode:
            return
        self.mode = mode
        self._apply()

    def toggle_mode(self) -> str:
        self.set_mode("dark" if self.mode == "light" else "light")
        return self.mode

    def _apply(self) -> None:
        if self.base == "sv":
            try:
                import sv_ttk

                sv_ttk.set_theme(self.mode)
            except Exception as exc:  # pragma: no cover
                log.info("sv-ttk 不可用，回退到自绘主题：%s", exc)
                self.base = "clam"
        if self.base == "clam":
            self._apply_native_base()
        self._apply_custom_styles()
        self._apply_option_database()

    def _apply_native_base(self) -> None:
        """在 clam 上重新配色（这是不装任何东西时用户看到的样子）。"""
        c = self.colors
        style = self.style
        style.theme_use("clam")
        style.configure(
            ".",
            background=c["bg"],
            foreground=c["text"],
            fieldbackground=c["surface"],
            bordercolor=c["border"],
            lightcolor=c["surface"],
            darkcolor=c["surface"],
            troughcolor=c["surface_alt"],
            focuscolor=c["accent"],
            font=self.font(10),
        )
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("TButton", padding=(12, 6), relief="flat", borderwidth=1)
        style.map(
            "TButton",
            background=[
                ("pressed", c["surface_hover"]),
                ("active", c["surface_alt"]),
                ("disabled", c["bg"]),
            ],
            foreground=[("disabled", c["faint"])],
            bordercolor=[("focus", c["accent"]), ("!focus", c["border"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=c["surface"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            padding=(8, 6),
        )
        style.map("TEntry", bordercolor=[("focus", c["accent"])])
        style.configure(
            "TCombobox",
            fieldbackground=c["surface"],
            background=c["surface"],
            foreground=c["text"],
            arrowcolor=c["muted"],
            bordercolor=c["border"],
            padding=(8, 5),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["surface"]), ("disabled", c["bg"])],
            bordercolor=[("focus", c["accent"])],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=c["surface"],
            background=c["surface"],
            foreground=c["text"],
            arrowcolor=c["muted"],
            bordercolor=c["border"],
            padding=(6, 4),
        )
        style.configure(
            "TCheckbutton", background=c["bg"], foreground=c["text"], focuscolor=c["accent"]
        )
        style.map(
            "TCheckbutton",
            background=[("active", c["bg"])],
            indicatorcolor=[
                ("selected", c["accent"]),
                ("!selected", c["surface"]),
            ],
        )
        style.configure(
            "TLabelframe",
            background=c["bg"],
            bordercolor=c["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label", background=c["bg"], foreground=c["muted"], font=self.font(9, "bold")
        )
        style.configure(
            "Treeview",
            background=c["surface"],
            fieldbackground=c["surface"],
            foreground=c["text"],
            bordercolor=c["border"],
            borderwidth=1,
            rowheight=27,
        )
        style.map(
            "Treeview",
            background=[("selected", c["selection_bg"])],
            foreground=[("selected", c["selection_fg"])],
        )
        style.configure(
            "Treeview.Heading",
            background=c["surface_alt"],
            foreground=c["muted"],
            relief="flat",
            borderwidth=0,
            padding=(8, 7),
            font=self.font(9, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", c["surface_hover"])])
        style.configure("TSeparator", background=c["border"])
        style.configure(
            "Horizontal.TProgressbar",
            background=c["accent"],
            troughcolor=c["surface_alt"],
            bordercolor=c["surface_alt"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            thickness=8,
            borderwidth=0,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=c["surface_alt"],
            troughcolor=c["bg"],
            bordercolor=c["bg"],
            arrowcolor=c["muted"],
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", c["border_strong"]), ("pressed", c["muted"])],
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=c["surface_alt"],
            troughcolor=c["bg"],
            bordercolor=c["bg"],
            arrowcolor=c["muted"],
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Horizontal.TScrollbar",
            background=[("active", c["border_strong"]), ("pressed", c["muted"])],
        )
        # 去掉滚动条两端的箭头，观感立刻干净很多
        for orient in ("Vertical", "Horizontal"):
            style.layout(
                f"{orient}.TScrollbar",
                [
                    (
                        f"{orient}.Scrollbar.trough",
                        {
                            "sticky": "ns" if orient == "Vertical" else "ew",
                            "children": [
                                (
                                    f"{orient}.Scrollbar.thumb",
                                    {"expand": "1", "sticky": "nswe"},
                                )
                            ],
                        },
                    )
                ],
            )
        style.configure("TPanedwindow", background=c["bg"])
        style.configure("Sash", sashthickness=7, gripcount=0, background=c["bg"], bordercolor=c["bg"])
        style.configure("TNotebook", background=c["bg"], bordercolor=c["border"], tabmargins=(8, 6, 8, 0))
        style.configure(
            "TNotebook.Tab",
            background=c["surface_alt"],
            foreground=c["muted"],
            padding=(14, 7),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", c["surface"])],
            foreground=[("selected", c["text"])],
        )
        self._install_check_indicator()

    def _install_check_indicator(self) -> None:
        """把主题自带的勾选框指示器换成自绘图片。

        部分 Tk 版本（本机 8.6.15 实测）的 clam 主题会把**已勾选**状态渲染成
        一个粗叉 —— 既难看又容易被当成「错误」。这里用 ``element_create``
        换上自己的图片：勾选是强调色圆角块 + 白色对勾，未勾选是中灰描边空框。

        图片缺失或 API 不可用时静默回退到主题自带外观，不影响可用性。
        """
        on = self.image("check_on.png")
        off = self.image("check_off.png")
        if on is None or off is None:
            log.debug("缺少 check_on/check_off 图片，沿用主题默认勾选框")
            return
        try:
            if "Gal.Checkbutton.indicator" not in self.style.element_names():
                self.style.element_create(
                    "Gal.Checkbutton.indicator",
                    "image",
                    str(off),
                    ("selected", str(on)),
                    sticky="w",
                    padding=(0, 0, 7, 0),
                )
            self.style.layout(
                "TCheckbutton",
                [
                    (
                        "Checkbutton.padding",
                        {
                            "sticky": "nswe",
                            "children": [
                                ("Gal.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                                (
                                    "Checkbutton.focus",
                                    {
                                        "side": "left",
                                        "sticky": "w",
                                        "children": [
                                            ("Checkbutton.label", {"sticky": "nswe"}),
                                        ],
                                    },
                                ),
                            ],
                        },
                    )
                ],
            )
        except tk.TclError as exc:  # pragma: no cover - 取决于 Tk 版本
            log.debug("自定义勾选框指示器失败，沿用主题默认外观：%s", exc)

    def _apply_custom_styles(self) -> None:
        """项目自己的样式名，统一加 ``Gal.`` 前缀避免和主题库撞车。"""
        c = self.colors
        style = self.style

        style.configure("Gal.Card.TFrame", background=c["surface"], relief="flat")
        style.configure("Gal.Header.TFrame", background=c["surface"], relief="flat")
        style.configure("Gal.Surface.TFrame", background=c["surface"], relief="flat")
        style.configure("Gal.Bg.TFrame", background=c["bg"], relief="flat")

        # 品牌栏上的文字：背景跟着头部卡片走，不能用地板色
        style.configure(
            "Gal.HeaderTitle.TLabel",
            background=c["surface"],
            foreground=c["text"],
            font=self.font(15, "bold"),
        )
        style.configure(
            "Gal.HeaderSub.TLabel",
            background=c["surface"],
            foreground=c["muted"],
            font=self.font(9),
        )
        style.configure("Gal.Header.TLabel", background=c["surface"], foreground=c["text"])
        style.configure(
            "Gal.Version.TLabel",
            background=c["accent_soft"],
            foreground=c["accent"],
            font=self.font(8, "bold"),
            padding=(7, 2),
        )

        style.configure("Gal.Title.TLabel", font=self.font(15, "bold"), foreground=c["text"])
        style.configure("Gal.Subtitle.TLabel", font=self.font(9), foreground=c["muted"])
        style.configure("Gal.Heading.TLabel", font=self.font(11, "bold"), foreground=c["text"])
        style.configure("Gal.Muted.TLabel", foreground=c["muted"])
        style.configure("Gal.Faint.TLabel", foreground=c["faint"], font=self.font(9))
        style.configure("Gal.Accent.TLabel", foreground=c["accent"], font=self.font(10, "bold"))
        style.configure("Gal.Status.TLabel", foreground=c["muted"], font=self.font(9))
        style.configure("Gal.Badge.TLabel", foreground=c["accent"], font=self.font(9, "bold"))

        # 强调按钮：主操作（开始扫描 / 导出）
        style.configure(
            "Gal.Accent.TButton",
            background=c["accent"],
            foreground=c["accent_text"],
            bordercolor=c["accent"],
            focuscolor=c["accent"],
            padding=(16, 7),
            relief="flat",
            borderwidth=1,
            font=self.font(10, "bold"),
        )
        style.map(
            "Gal.Accent.TButton",
            background=[
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
                ("disabled", c["surface_alt"]),
            ],
            foreground=[("disabled", c["faint"])],
            bordercolor=[("disabled", c["surface_alt"])],
        )

        # 幽灵按钮：次要操作，平时和背景融为一体
        style.configure(
            "Gal.Ghost.TButton",
            background=c["bg"],
            foreground=c["muted"],
            bordercolor=c["bg"],
            padding=(9, 6),
            relief="flat",
            borderwidth=1,
        )
        style.map(
            "Gal.Ghost.TButton",
            background=[("pressed", c["surface_hover"]), ("active", c["surface_alt"])],
            foreground=[("active", c["text"])],
        )

        style.configure(
            "Gal.Danger.TButton",
            background=c["bg"],
            foreground=c["danger"],
            bordercolor=c["border"],
            padding=(12, 6),
            relief="flat",
            borderwidth=1,
        )
        style.map(
            "Gal.Danger.TButton",
            background=[("pressed", c["danger"]), ("active", c["surface_alt"])],
            foreground=[("active", c["danger"])],
        )

        # 工具栏按钮：图标 + 文字，紧凑排布
        style.configure(
            "Gal.Tool.TButton",
            background=c["surface"],
            foreground=c["text"],
            bordercolor=c["surface"],
            padding=(6, 4),
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Gal.Tool.TButton",
            background=[("pressed", c["surface_hover"]), ("active", c["surface_alt"])],
            bordercolor=[("active", c["border"])],
        )

        style.configure("Gal.Vertical.TSeparator", background=c["border"])
        style.configure("Gal.Horizontal.TSeparator", background=c["border"])
        style.configure(
            "Gal.Horizontal.TProgressbar",
            background=c["accent"],
            troughcolor=c["surface_alt"],
            bordercolor=c["surface_alt"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            thickness=8,
            borderwidth=0,
        )

    def _apply_option_database(self) -> None:
        """有些控件是经典 Tk 组件，样式只能通过 option database 设置。"""
        c = self.colors
        try:
            self.root.option_add("*TCombobox*Listbox.background", c["surface"])
            self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", c["accent_text"])
            self.root.option_add("*TCombobox*Listbox.borderWidth", 0)
            self.root.option_add("*Menu.background", c["surface"])
            self.root.option_add("*Menu.foreground", c["text"])
            self.root.option_add("*Menu.activeBackground", c["accent"])
            self.root.option_add("*Menu.activeForeground", c["accent_text"])
            self.root.option_add("*Menu.borderWidth", 0)
            self.root.option_add("*Menu.relief", "flat")
            self.root.option_add("*Text.selectBackground", c["selection_bg"])
            self.root.option_add("*Text.selectForeground", c["selection_fg"])
        except Exception:  # pragma: no cover
            pass


def describe_theme() -> str:
    """给「关于」对话框用的一句话说明。"""
    base = available_base_theme()
    return (
        "Fluent 风格（sv-ttk 增强）"
        if base == "sv"
        else "内置自绘主题（零依赖）"
    )

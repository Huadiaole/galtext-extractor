"""图形界面（Tkinter，纯标准库）。

布局::

    ┌──────────────────────────────────────────────────────────────┐
    │ ◆ GalText Extractor                                    ☀/☾  │  品牌栏
    ├──────────────────────────────────────────────────────────────┤
    │ 游戏目录 [____________] [📁浏览] [▶开始扫描] [■停止]          │
    ├──────────────────────────────────────────────────────────────┤
    │ 解析器 ☑KiriKiri ☑NScripter ☐通用扫描  去重 ☑  最小长度 [2]   │
    ├───────────────────────┬──────────────────────────────────────┤
    │ 脚本列表              │ 文本预览（可直接搜索 / 全选复制）     │
    │ 引擎 | 路径 | 行数    │                                      │
    ├───────────────────────┴──────────────────────────────────────┤
    │ 格式[CSV▾] 编码[▾] [导出][复制]  ▓▓▓▓░░ 状态栏                │
    └──────────────────────────────────────────────────────────────┘

扫描在工作线程里跑，只通过 ``queue.Queue`` 往主线程回传消息，
保证不违反 Tk 的单线程约束。

观感由 :mod:`galtext.theme` 统一提供：装了 ``sv-ttk`` 就是 Fluent 风格，
没装则用内置的自绘主题，两种情况下都支持明暗切换。
"""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import (
    __app_name__,
    __author__,
    __license__,
    __version__,
    exporter,
    parsers,
    pipeline,
    scriptpdf,
    textkit,
)
from . import theme as theme_mod
from .common import ScanResult, human_size

log = logging.getLogger(__name__)

MAX_PREVIEW_LINES = 20_000
POLL_INTERVAL_MS = 80
AUTO_FONT_LABEL = "自动（推荐）"
WINDOW_TITLE = f"{__app_name__} {__version__} —— galgame 文本提取器"


def _enable_dpi_awareness() -> None:
    """让界面在高分屏上不发虚（失败无所谓）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


def _pick_font_family(root: tk.Misc) -> str:
    """兼容旧调用点；真正的字体挑选在 :mod:`galtext.theme` 里。"""
    return theme_mod.pick_font_family(root)


class GalTextApp:
    """主窗口。"""

    def __init__(self, master: tk.Tk, initial_root: str | None = None) -> None:
        self.master = master
        self.result: ScanResult | None = None
        self.lines_by_source: dict[str, list[int]] = {}
        self.visible_lines: list[int] = []
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._current_scope = "all"
        self._icon_widgets: list[tuple[tk.Widget, str, bool]] = []
        self._plain_widgets: list[tuple[tk.Widget, str]] = []

        master.title(WINDOW_TITLE)
        self._apply_initial_geometry(master)
        master.minsize(900, 560)

        # 主题必须最先建立：后面所有控件都要用它提供的样式、配色与图标
        self.theme = theme_mod.Theme(master, mode=self._initial_mode())
        self._ui_font = self.theme.font(10)
        self.theme.apply_window_icon(master)

        self._build_ui()
        self._retint()
        self._poll_queue()
        # 枚举系统中文字体要遍历并解析整个字体目录，放后台做，别卡住启动
        self.master.after(400, self._populate_fonts_async)

        if initial_root:
            self.dir_var.set(initial_root)
            self.master.after(300, self._on_scan)

    @staticmethod
    def _initial_mode() -> str:
        """初始明暗模式，可用环境变量 ``GALTEXT_MODE`` 强制指定。"""
        forced = os.environ.get("GALTEXT_MODE", "").strip().lower()
        return forced if forced in theme_mod.PALETTES else "light"

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_initial_geometry(master: tk.Tk) -> None:
        """按屏幕大小挑一个放得下的初始尺寸并居中。

        写死 ``1180x760`` 在 1366x768 这类小屏笔记本上会把底部的
        「导出 / 进度 / 状态栏」挤出屏幕外，所以这里必须按实际屏幕收窄。
        """
        try:
            screen_w = master.winfo_screenwidth()
            screen_h = master.winfo_screenheight()
        except tk.TclError:  # pragma: no cover
            master.geometry("1100x720")
            return
        width = max(880, min(1180, screen_w - 90))
        height = max(540, min(800, screen_h - 140))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        master.geometry(f"{width}x{height}+{x}+{y}")

    def _init_style(self) -> None:  # pragma: no cover - 兼容旧调用点
        """已由 :class:`galtext.theme.Theme` 取代，保留空实现以免外部代码报错。"""
        return

    # ------------------------------------------------------------------
    # 图标与主题刷新
    # ------------------------------------------------------------------
    def _attach_icon(
        self, widget: tk.Widget, name: str, on_accent: bool = False
    ) -> tk.Widget:
        """给按钮（或标签）挂一个图标，并登记下来以便切换主题时换描边色。"""
        image = self.theme.icon_on_accent(name) if on_accent else self.theme.icon(name)
        if image is not None:
            try:
                widget.configure(image=image, compound="left")  # type: ignore[call-arg]
                self._icon_widgets.append((widget, name, on_accent))
            except tk.TclError:  # pragma: no cover
                pass
        return widget

    def _retint(self) -> None:
        """把主题色刷到那些不归 ttk 样式管的控件上。

        ttk 的部分（按钮、表格、滚动条）由 ``theme.apply()`` 处理；
        这里负责经典 Tk 组件（``Text``）和图标描边色。
        """
        c = self.theme.colors
        try:
            self.master.configure(bg=c["bg"])
        except tk.TclError:  # pragma: no cover
            pass

        for widget, name, on_accent in list(self._icon_widgets):
            image = self.theme.icon_on_accent(name) if on_accent else self.theme.icon(name)
            if image is None:
                continue
            try:
                widget.configure(image=image)  # type: ignore[call-arg]
            except tk.TclError:  # pragma: no cover
                self._icon_widgets.remove((widget, name, on_accent))

        text = getattr(self, "text", None)
        if text is not None:
            text.configure(
                background=c["surface"],
                foreground=c["text"],
                insertbackground=c["text"],
                selectbackground=c["selection_bg"],
                selectforeground=c["selection_fg"],
                highlightthickness=1,
                highlightbackground=c["border"],
                highlightcolor=c["accent"],
            )
            text.tag_configure("speaker", foreground=c["speaker"])
            text.tag_configure("meta", foreground=c["narration"])
            text.tag_configure("hit", background=c["highlight"], foreground=c["text"])

        for widget, color_key in list(self._plain_widgets):
            try:
                widget.configure(background=self.theme.color(color_key))
            except tk.TclError:  # pragma: no cover
                self._plain_widgets.remove((widget, color_key))

        toggle = getattr(self, "_mode_button", None)
        if toggle is not None:
            icon_name = "moon" if self.theme.mode == "light" else "sun"
            image = self.theme.icon(icon_name)
            label = "深色" if self.theme.mode == "light" else "浅色"
            try:
                if image is not None:
                    toggle.configure(image=image, text=f" {label}")
                else:
                    toggle.configure(text=label)
            except tk.TclError:  # pragma: no cover
                pass
            # 图标换了，登记项也要跟着换名字
            self._icon_widgets = [
                (w, icon_name if w is toggle else n, a)
                for (w, n, a) in self._icon_widgets
            ]

        logo_label = getattr(self, "_logo_label", None)
        if logo_label is not None:
            image = self.theme.app_icon(48)
            if image is not None:
                try:
                    logo_label.configure(image=image)
                except tk.TclError:  # pragma: no cover
                    pass

    def _on_toggle_mode(self) -> None:
        self._set_mode("dark" if self.theme.mode == "light" else "light")

    def _plain(self, widget: tk.Widget, color_key: str) -> tk.Widget:
        """登记一个用纯色填充的经典 Tk 控件，切换主题时重新上色。"""
        try:
            widget.configure(background=self.theme.color(color_key))
        except tk.TclError:  # pragma: no cover
            pass
        self._plain_widgets.append((widget, color_key))
        return widget

    def _build_ui(self) -> None:
        self.master.columnconfigure(0, weight=1)
        # 只有工作区那一行可以伸缩，其余行保持自然高度 ——
        # 否则小屏上底部工具条会被挤出窗口外。
        self.master.rowconfigure(3, weight=1)

        self._build_header()
        self._build_path_bar()
        self._build_options()
        self._build_workspace()
        self._build_status_bar()
        self._build_menu()

    # ---- 品牌栏 --------------------------------------------------------
    def _build_header(self) -> None:
        header = ttk.Frame(self.master, style="Gal.Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        inner = ttk.Frame(header, style="Gal.Header.TFrame", padding=(16, 12, 16, 12))
        inner.grid(row=0, column=0, sticky="ew")
        inner.columnconfigure(3, weight=1)

        self._logo_label = ttk.Label(inner, style="Gal.Header.TLabel")
        logo = self.theme.app_icon(48)
        if logo is not None:
            self._logo_label.configure(image=logo)
        self._logo_label.grid(row=0, column=0, rowspan=2, padx=(0, 14))

        ttk.Label(inner, text=__app_name__, style="Gal.HeaderTitle.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(
            inner,
            text="从 galgame 目录提取对话，排版成剧本 PDF",
            style="Gal.HeaderSub.TLabel",
        ).grid(row=1, column=1, sticky="nw", pady=(2, 0))
        ttk.Label(inner, text=f"v{__version__}", style="Gal.Version.TLabel").grid(
            row=0, column=2, sticky="sw", padx=(10, 0)
        )

        actions = ttk.Frame(inner, style="Gal.Header.TFrame")
        actions.grid(row=0, column=4, rowspan=2, sticky="e")

        self._mode_button = ttk.Button(
            actions, text=" 深色", style="Gal.Tool.TButton", command=self._on_toggle_mode
        )
        self._mode_button.pack(side="left")
        self._attach_icon(self._mode_button, "moon")

        engines_btn = ttk.Button(
            actions, text=" 解析器", style="Gal.Tool.TButton", command=self._show_engines
        )
        engines_btn.pack(side="left", padx=(10, 0))
        self._attach_icon(engines_btn, "grid")

        about_btn = ttk.Button(
            actions, text=" 关于", style="Gal.Tool.TButton", command=self._show_about
        )
        about_btn.pack(side="left", padx=(10, 0))
        self._attach_icon(about_btn, "info")

        self._plain(tk.Frame(header, height=1), "border").grid(row=1, column=0, sticky="ew")

    # ---- 目录与主操作 --------------------------------------------------
    def _build_path_bar(self) -> None:
        bar = ttk.Frame(self.master, style="Gal.Bg.TFrame", padding=(16, 14, 16, 4))
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="游戏目录", style="Gal.Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )

        self.dir_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.dir_var, font=self.theme.font(10))
        entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), ipady=1)
        entry.bind("<Return>", lambda _e: self._on_scan())

        browse_btn = ttk.Button(bar, text=" 浏览", command=self._on_browse)
        browse_btn.grid(row=0, column=2, padx=(0, 6))
        self._attach_icon(browse_btn, "folder")

        self.scan_btn = ttk.Button(
            bar, text=" 开始扫描", style="Gal.Accent.TButton", command=self._on_scan
        )
        self.scan_btn.grid(row=0, column=3, padx=(0, 6))
        self._attach_icon(self.scan_btn, "scan", on_accent=True)

        self.cancel_btn = ttk.Button(
            bar,
            text=" 停止",
            style="Gal.Ghost.TButton",
            command=self._on_cancel,
            state="disabled",
        )
        self.cancel_btn.grid(row=0, column=4)
        self._attach_icon(self.cancel_btn, "stop")

    # ---- 扫描选项 ------------------------------------------------------
    def _build_options(self) -> None:
        opts = ttk.LabelFrame(self.master, text=" 扫描选项 ", padding=(14, 10))
        opts.grid(row=2, column=0, sticky="ew", padx=16)

        ttk.Label(opts, text="解析器", style="Gal.Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.engine_vars: dict[str, tk.BooleanVar] = {}
        column = 1
        for key, name in pipeline.engine_choices():
            if key in ("plaintext", "generic"):
                continue
            var = tk.BooleanVar(value=True)
            self.engine_vars[key] = var
            ttk.Checkbutton(opts, text=name, variable=var).grid(
                row=0, column=column, sticky="w", padx=(0, 10)
            )
            column += 1

        self.generic_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="通用二进制扫描（慢）", variable=self.generic_var
        ).grid(row=0, column=column, sticky="w", padx=(0, 14))
        column += 1

        self.dedupe_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="去重", variable=self.dedupe_var).grid(
            row=0, column=column, sticky="w", padx=(0, 14)
        )
        column += 1

        self.aggressive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="激进模式", variable=self.aggressive_var).grid(
            row=0, column=column, sticky="w", padx=(0, 14)
        )
        column += 1

        self.keep_ascii_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="保留纯英文行", variable=self.keep_ascii_var).grid(
            row=0, column=column, sticky="w"
        )
        column += 1

        self.chinese_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="只保留中文行", variable=self.chinese_only_var
        ).grid(row=0, column=column, sticky="w", padx=(14, 0))
        column += 1

        self.override_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="封包覆盖（补丁优先）", variable=self.override_var
        ).grid(row=0, column=column, sticky="w", padx=(14, 0))

        ttk.Label(opts, text="最短长度", style="Gal.Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 8)
        )
        self.min_len_var = tk.IntVar(value=2)
        ttk.Spinbox(
            opts, from_=1, to=200, width=5, textvariable=self.min_len_var
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))

        ttk.Label(opts, text="最长长度", style="Gal.Muted.TLabel").grid(
            row=1, column=2, sticky="e", pady=(8, 0), padx=(0, 8)
        )
        self.max_len_var = tk.IntVar(value=400)
        ttk.Spinbox(
            opts, from_=10, to=5000, width=6, textvariable=self.max_len_var
        ).grid(row=1, column=3, sticky="w", pady=(8, 0))

        # --- PDF 相关设置（导出剧本 PDF 时生效） ---
        self._pdf_font_map: dict[str, tuple[str, int]] = {}
        # PDF 选项单独放一行子框架：opts 的网格列宽被上面的控件撑着，
        # 直接共用会出现很大的空档。
        pdf_row = ttk.Frame(opts, style="Gal.Bg.TFrame")
        pdf_row.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        pdf_row.columnconfigure(4, weight=1)

        ttk.Label(pdf_row, text="PDF 字体", style="Gal.Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.pdf_font_var = tk.StringVar(value=AUTO_FONT_LABEL)
        self.pdf_font_box = ttk.Combobox(
            pdf_row,
            textvariable=self.pdf_font_var,
            values=[AUTO_FONT_LABEL],
            state="readonly",
            width=34,
        )
        self.pdf_font_box.grid(row=0, column=1, sticky="w")

        font_btn = ttk.Button(pdf_row, text=" 选择字体文件", command=self._on_pick_font)
        font_btn.grid(row=0, column=2, sticky="w", padx=(8, 18))
        self._attach_icon(font_btn, "font")

        self.scene_per_page_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            pdf_row, text="每场景另起一页", variable=self.scene_per_page_var
        ).grid(row=0, column=3, sticky="w")

        # 说话人相关：很多脚本把名字放在单独一行声明，不跟踪就会丢掉人名
        speaker_row = ttk.Frame(opts, style="Gal.Bg.TFrame")
        speaker_row.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(8, 0))

        self.track_speakers_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            speaker_row,
            text="跟踪说话人声明",
            variable=self.track_speakers_var,
        ).grid(row=0, column=0, sticky="w")

        self.guess_speakers_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            speaker_row,
            text="自动识别单独成行的角色名",
            variable=self.guess_speakers_var,
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))

        ttk.Label(speaker_row, text="旁白标记", style="Gal.Muted.TLabel").grid(
            row=0, column=2, sticky="e", padx=(20, 6)
        )
        self.narration_var = tk.StringVar(value="")
        narration_entry = ttk.Entry(speaker_row, textvariable=self.narration_var, width=10)
        narration_entry.grid(row=0, column=3, sticky="w")
        ttk.Label(
            speaker_row,
            text="（填「旁白」可让每行都带名字；留空则旁白不署名）",
            style="Gal.Faint.TLabel",
        ).grid(row=0, column=4, sticky="w", padx=(8, 0))

    # ---- 工作区（脚本列表 + 文本预览）-----------------------------------
    def _build_workspace(self) -> None:
        paned = ttk.PanedWindow(self.master, orient="horizontal")
        paned.grid(row=3, column=0, sticky="nsew", padx=16, pady=(12, 6))

        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=1)

        ttk.Label(left, text="发现的脚本", style="Gal.Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        columns = ("engine", "path", "lines", "encoding")
        # height 必须显式给个小值：Treeview 默认 10 行、Text 默认 24 行，
        # 两者叠加会让「内容最小高度」超过小屏窗口高度，把底部工具条挤出屏幕。
        self.tree = ttk.Treeview(
            left, columns=columns, show="headings", selectmode="browse", height=8
        )
        for key, label, width, anchor in (
            ("engine", "引擎", 152, "w"),
            ("path", "路径 / 封包成员", 290, "w"),
            ("lines", "行数", 60, "e"),
            ("encoding", "编码", 80, "w"),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "path"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_script)

        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        right = ttk.Frame(paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        paned.add(right, weight=3)

        search_bar = ttk.Frame(right, style="Gal.Bg.TFrame")
        search_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_bar.columnconfigure(2, weight=1)

        search_icon = ttk.Label(search_bar, style="Gal.Muted.TLabel")
        search_icon.grid(row=0, column=0, padx=(0, 8))
        self._attach_icon(search_icon, "search")
        ttk.Label(search_bar, text="搜索", style="Gal.Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )

        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_bar, textvariable=self.search_var)
        search_entry.grid(row=0, column=2, sticky="ew", padx=(0, 6))
        search_entry.bind("<Return>", lambda _e: self._refresh_preview())
        self.search_var.trace_add("write", lambda *_a: self._schedule_preview())
        ttk.Button(search_bar, text="清除", command=self._clear_search).grid(
            row=0, column=3
        )

        info = ttk.Frame(right)
        info.grid(row=1, column=0, sticky="ew")
        info.columnconfigure(0, weight=1)
        self.scope_label = ttk.Label(info, text="尚未扫描", style="Gal.Muted.TLabel")
        self.scope_label.grid(row=0, column=0, sticky="w")

        text_frame = ttk.Frame(right)
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self.text = tk.Text(
            text_frame,
            wrap="word",
            undo=False,
            font=self._ui_font,
            padx=8,
            pady=6,
            spacing1=1,
            spacing3=2,
            height=12,
            width=48,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        self.text.tag_configure("speaker", foreground="#1a5fb4")
        self.text.tag_configure("meta", foreground="#888888")
        self.text.tag_configure("hit", background="#ffe066")
        self.text.configure(state="disabled")

        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        text_scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=text_scroll.set)

        self.text.bind("<Control-a>", self._select_all)
        self.text.bind("<Control-A>", self._select_all)

    # ---- 底部：导出与状态栏 ---------------------------------------------
    def _build_status_bar(self) -> None:
        bottom = ttk.Frame(self.master, style="Gal.Bg.TFrame", padding=(16, 6, 16, 10))
        bottom.grid(row=4, column=0, sticky="ew")
        # 可伸缩的空列放在最右边，其余控件一律靠左排 ——
        # 这样进度条不会被挤出窗口边缘，也不会在中间留出奇怪的间隙。
        bottom.columnconfigure(8, weight=1)

        ttk.Label(bottom, text="格式", style="Gal.Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.format_var = tk.StringVar(value="csv")
        format_box = ttk.Combobox(
            bottom,
            textvariable=self.format_var,
            values=[exporter.FORMAT_LABELS[f] for f in exporter.ALL_FORMATS],
            state="readonly",
            width=18,
        )
        format_box.grid(row=0, column=1, padx=(0, 8))
        format_box.set(exporter.FORMAT_LABELS["csv"])

        ttk.Label(bottom, text="编码", style="Gal.Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        self.encoding_var = tk.StringVar(value=exporter.DEFAULT_ENCODING)
        ttk.Combobox(
            bottom,
            textvariable=self.encoding_var,
            values=list(exporter.ENCODINGS),
            state="readonly",
            width=12,
        ).grid(row=0, column=3, padx=(0, 8))

        self.only_filtered_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bottom, text="只导出当前筛选结果", variable=self.only_filtered_var
        ).grid(row=0, column=4, padx=(0, 8))

        export_btn = ttk.Button(
            bottom, text=" 导出", style="Gal.Accent.TButton", command=self._on_export
        )
        export_btn.grid(row=0, column=5)
        self._attach_icon(export_btn, "export", on_accent=True)

        copy_btn = ttk.Button(bottom, text=" 复制全部", command=self._on_copy_all)
        copy_btn.grid(row=0, column=6, padx=(8, 0))
        self._attach_icon(copy_btn, "copy")

        self.progress = ttk.Progressbar(
            bottom, mode="determinate", maximum=100, length=170,
            style="Gal.Horizontal.TProgressbar",
        )
        self.progress.grid(row=0, column=7, padx=(16, 0))
        ttk.Frame(bottom, style="Gal.Bg.TFrame").grid(row=0, column=8, sticky="ew")

        self._plain(tk.Frame(bottom, height=1), "border").grid(
            row=1, column=0, columnspan=9, sticky="ew", pady=(10, 8)
        )
        self.status_var = tk.StringVar(value="就绪：选择一个游戏目录，然后点「开始扫描」。")
        ttk.Label(bottom, textvariable=self.status_var, style="Gal.Status.TLabel").grid(
            row=2, column=0, columnspan=9, sticky="w"
        )

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.master)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="选择游戏目录…", command=self._on_browse)
        file_menu.add_command(label="开始扫描", command=self._on_scan)
        file_menu.add_separator()
        for fmt in exporter.ALL_FORMATS:
            file_menu.add_command(
                label=f"导出为 {exporter.FORMAT_LABELS[fmt]}",
                command=lambda f=fmt: self._on_export(f),
            )
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.master.destroy)
        menubar.add_cascade(label="文件", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="切换浅色 / 深色", command=self._on_toggle_mode)
        self.mode_var = tk.StringVar(value=self.theme.mode)
        for mode, label in theme_mod.MODE_LABELS.items():
            view_menu.add_radiobutton(
                label=f"{label}模式",
                value=mode,
                variable=self.mode_var,
                command=lambda m=mode: self._set_mode(m),
            )
        view_menu.add_separator()
        view_menu.add_command(label="清空结果", command=self._on_clear_results)
        menubar.add_cascade(label="视图", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="支持的解析器…", command=self._show_engines)
        help_menu.add_command(label="使用说明", command=self._show_usage)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.master.config(menu=menubar)

    def _set_mode(self, mode: str) -> None:
        if mode == self.theme.mode:
            return
        self.theme.set_mode(mode)
        self.mode_var.set(mode)
        self.theme.apply_window_icon(self.master)
        self._retint()
        self.status_var.set(
            f"已切换到{theme_mod.MODE_LABELS.get(mode, mode)}模式。"
        )

    def _on_clear_results(self) -> None:
        self.result = None
        self.lines_by_source.clear()
        self.visible_lines = []
        self.tree.delete(*self.tree.get_children())
        self.scope_label.configure(text="尚未扫描")
        self._set_text("")
        self.progress.configure(value=0)
        self.status_var.set("已清空结果。选择一个游戏目录，然后点「开始扫描」。")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_browse(self) -> None:
        initial = self.dir_var.get() or str(pathlib.Path.home())
        chosen = filedialog.askdirectory(title="选择游戏目录", initialdir=initial)
        if chosen:
            self.dir_var.set(chosen)

    # ------------------------------------------------------------------
    # PDF 字体
    # ------------------------------------------------------------------
    def _populate_fonts_async(self) -> None:
        def worker() -> None:
            try:
                fonts = scriptpdf.available_fonts()
            except Exception as exc:  # pragma: no cover
                log.warning("枚举字体失败：%s", exc)
                fonts = []
            self._queue.put(("fonts", fonts))

        threading.Thread(target=worker, daemon=True, name="galtext-fonts").start()

    def _apply_font_list(self, fonts: list) -> None:
        labels = [AUTO_FONT_LABEL]
        for info in fonts:
            label = f"{info.get('name') or pathlib.Path(info['path']).stem}（{pathlib.Path(info['path']).name}）"
            self._pdf_font_map[label] = (str(info["path"]), int(info.get("face_index", 0)))
            labels.append(label)
        self.pdf_font_box.configure(values=labels)
        if len(labels) > 1:
            self.status_var.set(f"已找到 {len(labels) - 1} 个可用中文字体，导出剧本 PDF 时可选用。")

    def _on_pick_font(self) -> None:
        path = filedialog.askopenfilename(
            title="选择字体文件",
            initialdir="C:/Windows/Fonts",
            filetypes=[("字体文件", "*.ttf *.ttc *.otf"), ("所有文件", "*.*")],
        )
        if not path:
            return
        label = f"{pathlib.Path(path).name}（手动选择）"
        self._pdf_font_map[label] = (path, 0)
        values = list(self.pdf_font_box.cget("values"))
        if label not in values:
            values.append(label)
            self.pdf_font_box.configure(values=values)
        self.pdf_font_var.set(label)

    def _pdf_options(self) -> scriptpdf.ScriptPdfOptions:
        label = self.pdf_font_var.get()
        font_path, face_index = self._pdf_font_map.get(label, ("", 0))
        title = pathlib.Path(self.result.root).name if self.result else "剧本"
        return scriptpdf.ScriptPdfOptions(
            title=title,
            font_path=font_path,
            font_face_index=face_index,
            scene_per_page=self.scene_per_page_var.get(),
        )

    def _collect_options(self) -> pipeline.ExtractOptions:
        chosen = [key for key, var in self.engine_vars.items() if var.get()]
        if not chosen:
            chosen = []  # 交给 pipeline 自动挑选
        clean = textkit.CleanOptions(
            min_len=max(1, int(self.min_len_var.get() or 2)),
            max_len=max(2, int(self.max_len_var.get() or 400)),
            drop_ascii_only=not self.keep_ascii_var.get(),
            aggressive=self.aggressive_var.get(),
            chinese_only=self.chinese_only_var.get(),
            track_speakers=self.track_speakers_var.get(),
            guess_bare_speakers=self.guess_speakers_var.get(),
            narration_speaker=self.narration_var.get().strip(),
        )
        return pipeline.ExtractOptions(
            engines=chosen or None,
            include_generic=self.generic_var.get(),
            dedupe=self.dedupe_var.get(),
            override_archives=self.override_var.get(),
            clean=clean,
        )

    def _on_scan(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        root = self.dir_var.get().strip().strip('"')
        if not root:
            messagebox.showinfo("提示", "请先选择游戏目录。")
            return
        path = pathlib.Path(root)
        if not path.is_dir():
            messagebox.showerror("目录不存在", f"找不到目录：\n{path}")
            return

        options = self._collect_options()
        self._cancel_event.clear()
        self.scan_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.status_var.set("正在扫描…")
        self.tree.delete(*self.tree.get_children())
        self._set_text("")
        self.result = None
        self.lines_by_source.clear()

        def worker() -> None:
            try:
                result = pipeline.scan(
                    path,
                    options,
                    progress=lambda pct, msg: self._queue.put(("progress", (pct, msg))),
                    should_cancel=self._cancel_event.is_set,
                )
                self._queue.put(("done", result))
            except Exception:
                self._queue.put(("error", traceback.format_exc()))

        self._worker = threading.Thread(target=worker, daemon=True, name="galtext-scan")
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._cancel_event.set()
            self.status_var.set("正在停止…")
            self.cancel_btn.configure(state="disabled")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    percent, message = payload  # type: ignore[misc]
                    if percent is not None:
                        self.progress.configure(value=percent * 100)
                    self.status_var.set(str(message))
                elif kind == "done":
                    self._apply_result(payload)  # type: ignore[arg-type]
                elif kind == "fonts":
                    self._apply_font_list(payload)  # type: ignore[arg-type]
                elif kind == "error":
                    self._finish_scan()
                    messagebox.showerror("出错了", str(payload))
        except queue.Empty:
            pass
        self.master.after(POLL_INTERVAL_MS, self._poll_queue)

    def _finish_scan(self) -> None:
        self.scan_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # 结果呈现
    # ------------------------------------------------------------------
    def _apply_result(self, result: ScanResult) -> None:
        self._finish_scan()
        self.result = result
        self.progress.configure(value=100)

        self.lines_by_source.clear()
        for position, line in enumerate(result.lines):
            self.lines_by_source.setdefault(line.source, []).append(position)

        self.tree.delete(*self.tree.get_children())
        if result.lines:
            self.tree.insert(
                "",
                "end",
                iid="all",
                values=("全部", f"{len(result.scripts)} 个脚本", len(result.lines), ""),
            )
        for index, script in enumerate(result.scripts):
            engine_label = parsers.engine_name(script.engine)
            if script.error:
                note = f"  ⚠ {script.error}"
            elif script.note:
                note = f"  · {script.note}"
            else:
                note = ""
            self.tree.insert(
                "",
                "end",
                iid=f"s{index}",
                values=(
                    engine_label,
                    script.virtual_path + note,
                    script.line_count,
                    script.encoding,
                ),
            )

        if result.lines:
            self.tree.selection_set("all")
            self._current_scope = "all"
            self._refresh_preview()
        else:
            self._set_text("没有提取到文本。\n\n可以试试：\n  · 勾选「通用二进制扫描」\n  · 勾选「激进模式」\n  · 换一个游戏目录")

        for warning in result.warnings:
            self.status_var.set(warning)
        if not result.warnings:
            self.status_var.set(pipeline.format_summary(result))

        if result.cancelled:
            self.status_var.set("已取消。" + self.status_var.get())

    def _on_select_script(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._current_scope = selection[0]
        self._refresh_preview()

    def _schedule_preview(self) -> None:
        if getattr(self, "_preview_job", None) is not None:
            try:
                self.master.after_cancel(self._preview_job)
            except Exception:  # pragma: no cover
                pass
        self._preview_job = self.master.after(220, self._refresh_preview)

    def _clear_search(self) -> None:
        self.search_var.set("")

    def _scope_lines(self) -> tuple[list[int], str]:
        if self.result is None:
            return [], ""
        if self._current_scope == "all" or not self._current_scope.startswith("s"):
            return list(range(len(self.result.lines))), "全部文本"
        try:
            index = int(self._current_scope[1:])
            script = self.result.scripts[index]
        except (ValueError, IndexError):
            return list(range(len(self.result.lines))), "全部文本"
        positions = self.lines_by_source.get(script.virtual_path, [])
        return positions, script.virtual_path

    def _refresh_preview(self) -> None:
        self._preview_job = None
        if self.result is None:
            return
        positions, scope_name = self._scope_lines()
        query = self.search_var.get().strip()
        lines = [self.result.lines[p] for p in positions]

        if query:
            lowered = query.lower()
            lines = [
                ln for ln in lines if lowered in ln.text.lower() or lowered in ln.speaker.lower()
            ]
            scope_text = f"{scope_name}｜筛选 “{query}”：{len(lines)} 条"
        else:
            scope_text = f"{scope_name}｜{len(lines)} 条"

        self.visible_lines = [ln.index - 1 for ln in lines[:MAX_PREVIEW_LINES]]
        self.scope_label.configure(text=scope_text)

        self._render_lines(lines)

        if query:
            self._highlight(query)

    def _render_lines(self, lines: list) -> None:
        """逐条插入预览，并按「说话人 / 旁白」上不同的颜色。

        直接拼一个大字符串插入会把配色全丢掉 —— 标签样式必须在插入时指定。
        """
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line in lines[:MAX_PREVIEW_LINES]:
            if line.speaker:
                self.text.insert("end", line.speaker, "speaker")
                self.text.insert("end", f"：{line.text}\n")
            else:
                self.text.insert("end", f"{line.text}\n", "meta")
        if len(lines) > MAX_PREVIEW_LINES:
            self.text.insert(
                "end",
                f"\n…… 仅预览前 {MAX_PREVIEW_LINES} 条（导出时仍是全部 {len(lines)} 条）\n",
                "meta",
            )
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _set_text(self, body: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if body:
            self.text.insert("1.0", body)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _highlight(self, query: str) -> None:
        self.text.tag_remove("hit", "1.0", "end")
        if not query:
            return
        start = "1.0"
        count = 0
        while True:
            position = self.text.search(query, start, nocase=True, stopindex="end")
            if not position:
                break
            end = f"{position}+{len(query)}c"
            self.text.tag_add("hit", position, end)
            start = end
            count += 1
            if count >= 3000:
                break

    def _select_all(self, _event: object = None) -> str:
        self.text.tag_add("sel", "1.0", "end")
        return "break"

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _current_format(self) -> str:
        label = self.format_var.get()
        for fmt, text in exporter.FORMAT_LABELS.items():
            if text == label:
                return fmt
        return "csv"

    def _lines_to_export(self) -> list:
        if self.result is None:
            return []
        if self.only_filtered_var.get():
            return [self.result.lines[i] for i in self.visible_lines]
        return list(self.result.lines)

    def _on_export(self, fmt: str | None = None) -> None:
        if self.result is None or not self.result.lines:
            messagebox.showinfo("提示", "还没有可导出的文本，先扫描一次吧。")
            return
        fmt = fmt or self._current_format()
        lines = self._lines_to_export()
        if not lines:
            messagebox.showinfo("提示", "当前筛选结果为空。")
            return

        root_name = pathlib.Path(self.result.root).name or "galtext"
        initial = str(pathlib.Path(self.result.root).parent / f"{root_name}{exporter.EXTENSIONS[fmt]}")
        path = filedialog.asksaveasfilename(
            title="导出文本",
            defaultextension=exporter.EXTENSIONS[fmt],
            initialfile=pathlib.Path(initial).name,
            initialdir=str(pathlib.Path(initial).parent),
            filetypes=[(exporter.FORMAT_LABELS[fmt], "*" + exporter.EXTENSIONS[fmt]), ("所有文件", "*.*")],
        )
        if not path:
            return
        pdf_result = None
        try:
            if fmt == exporter.PDF_FORMAT:
                pdf_result = exporter.export_pdf(
                    path, lines, options=self._pdf_options(), result=self.result
                )
                written = pdf_result.path
            else:
                written = exporter.export(
                    path,
                    lines,
                    fmt=fmt,
                    encoding=self.encoding_var.get(),
                    result=self.result,
                )
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return

        if pdf_result is not None:
            self.status_var.set(
                f"已导出剧本 PDF：{pdf_result.pages} 页 / "
                f"{human_size(pdf_result.size_bytes)}"
            )
            detail = (
                f"已写入：\n{written}\n\n"
                f"{pdf_result.pages} 页 ｜ {pdf_result.scenes} 个场景 ｜ "
                f"{pdf_result.lines} 条台词 ｜ {human_size(pdf_result.size_bytes)}\n"
                f"字体：{pdf_result.font_name}"
            )
            if pdf_result.missing_chars:
                detail += (
                    f"\n\n⚠ 该字体缺少 {len(pdf_result.missing_chars)} 个字符，"
                    "这些字在 PDF 中会缺失：\n"
                    + "".join(pdf_result.missing_chars[:40])
                )
            if messagebox.askyesno("导出完成", detail + "\n\n要打开所在文件夹吗？"):
                self._open_folder(written.parent)
            return

        self.status_var.set(f"已导出 {len(lines)} 条到 {written}")
        if messagebox.askyesno("导出完成", f"已写入：\n{written}\n\n要打开所在文件夹吗？"):
            self._open_folder(written.parent)

    @staticmethod
    def _open_folder(folder: pathlib.Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:  # pragma: no cover
            pass

    def _on_copy_all(self) -> None:
        if self.result is None or not self.result.lines:
            return
        lines = self._lines_to_export()
        payload = "\n".join(ln.display() for ln in lines)
        self.master.clipboard_clear()
        self.master.clipboard_append(payload)
        self.status_var.set(f"已复制 {len(lines)} 条到剪贴板。")

    # ------------------------------------------------------------------
    # 对话框
    # ------------------------------------------------------------------
    def _show_engines(self) -> None:
        rows = [f"{__app_name__} {__version__}", "", "当前可用的解析器：", ""]
        for info in parsers.list_engines():
            mark = "●" if info["available"] else "○"
            rows.append(f"  {mark}  {info['name']}")
            rows.append(
                f"      关键字 {info['key']}　"
                + ("可用" if info["available"] else f"不可用（{info['error']}）")
            )
        rows += [
            "",
            "在「扫描选项」里勾选即可控制启用哪些解析器；",
            "命令行用 --engines kirikiri,nscripter 只跑指定的几个。",
        ]
        messagebox.showinfo("支持的解析器", "\n".join(rows))

    def _show_usage(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1. 点「浏览」选中游戏根目录\n"
            "   （有 data.xp3 / nscript.dat / *.exe 的那一层）。\n\n"
            "2. 点「开始扫描」，左栏会出现发现的脚本。\n"
            "   点任意一项只看它的台词，点「全部」看全量。\n\n"
            "3. 右侧搜索框实时筛选，命中的部分会高亮。\n\n"
            "4. 底部选好格式与编码，点「导出」保存。\n"
            "   导出剧本 PDF 时，可以在「扫描选项」里挑字体。\n\n"
            "认不出引擎时：勾上「通用二进制扫描」并开「激进模式」。",
        )

    def _show_about(self) -> None:
        base = theme_mod.describe_theme()
        messagebox.showinfo(
            "关于",
            f"{__app_name__}  v{__version__}\n"
            f"{theme_mod.__doc__.splitlines()[0] if theme_mod.__doc__ else ''}\n\n"
            f"开发者：{__author__}\n"
            f"许可：{__license__}\n"
            f"界面：{base}（{'深色' if self.theme.mode == 'dark' else '浅色'}）\n\n"
            "从 galgame 目录解析脚本与封包，提取对话文本，\n"
            "支持预览、搜索、多格式导出，以及排版成剧本 PDF。\n\n"
            "纯 Python 标准库实现，无需安装任何运行时依赖。\n\n"
            "导出的文本仅用于个人学习与翻译用途，\n"
            "请勿传播游戏原始资源；游戏版权归原厂商所有。",
        )


def main(initial_root: str | None = None) -> int:
    _enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无图形环境
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        print("可以改用命令行：python -m galtext scan <游戏目录>", file=sys.stderr)
        return 1
    GalTextApp(root, initial_root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

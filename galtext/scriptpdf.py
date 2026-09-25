"""把提取出来的文本排成「剧本风格」的 PDF。

排版样式（纯中文）::

    ┌──────────────────────────────────────────────┐
    │  某游戏名                                     │   ← 标题
    │  引擎：KiriKiri / KAG ｜ 台词 3312 条          │   ← 元信息（灰）
    │  ══════════════════════════════════════════  │
    │                                              │
    │  场景 1 · prologue.ks                         │   ← 场景标题
    │  data.xp3/scenario/prologue.ks                │   ← 来源（小灰字）
    │  ──────────────────────────────────────────  │
    │                                              │
    │  悠斗                                         │   ← 角色名（蓝）
    │      早上好，前辈。今天天气也不错呢。          │   ← 台词（缩进）
    │                                              │
    │      天空湛蓝清澈。                            │   ← 旁白（灰）
    │                                              │
    │                        — 3 / 42 —            │   ← 页码
    └──────────────────────────────────────────────┘

换行遵守中文排版禁则：``。，、！？`` 之类不会跑到行首，
``（「『`` 之类不会留在行尾；连续的英文单词不会被从中间截断。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import pathlib
from typing import Iterable, Sequence

from . import pdfgen
from .common import ScanResult, TextLine

log = logging.getLogger(__name__)

#: 不能出现在行首的标点
NO_LINE_START = set("。，、；：？！）］｝〕〉》」』】〗〙〛”’…—～·,.!?;:)]}>%‰℃")
#: 不能出现在行尾的标点
NO_LINE_END = set("（［｛〔〈《「『【〖〘〚“‘([{<")

#: 自动挑字体时的优先级（按文件名匹配，越靠前越优先）
FONT_PREFERENCE = (
    "msyh.ttc",
    "msyh.ttf",
    "msyhl.ttc",
    "simhei.ttf",
    "deng.ttf",
    "simsun.ttc",
    "msjh.ttc",
    "simkai.ttf",
)


@dataclasses.dataclass
class ScriptPdfOptions:
    """剧本 PDF 的全部排版参数。"""

    title: str = ""
    subtitle: str = ""
    font_path: str = ""
    font_face_index: int = 0

    body_size: float = 10.5
    speaker_size: float = 10.0
    heading_size: float = 13.5
    meta_size: float = 8.0
    footer_size: float = 8.0

    margin_left: float = 70.0
    margin_right: float = 70.0
    margin_top: float = 72.0
    margin_bottom: float = 68.0

    line_spacing: float = 1.62
    indent_em: float = 1.6
    block_gap: float = 0.55
    scene_gap: float = 1.35

    text_color: tuple[float, float, float] = (0.11, 0.11, 0.13)
    speaker_color: tuple[float, float, float] = (0.09, 0.29, 0.55)
    narration_color: tuple[float, float, float] = (0.42, 0.42, 0.46)
    heading_color: tuple[float, float, float] = (0.05, 0.05, 0.06)
    meta_color: tuple[float, float, float] = (0.55, 0.55, 0.58)
    rule_color: tuple[float, float, float] = (0.78, 0.78, 0.80)

    show_scene_headings: bool = True
    show_source_path: bool = True
    show_title_block: bool = True
    show_footer: bool = True
    scene_per_page: bool = False
    """每个场景另起一页（适合逐场景校对）。"""

    def clone(self, **kwargs) -> "ScriptPdfOptions":
        return dataclasses.replace(self, **kwargs)


@dataclasses.dataclass
class PdfResult:
    path: pathlib.Path
    pages: int
    lines: int
    scenes: int
    missing_chars: list[str]
    font_name: str
    font_path: str
    size_bytes: int

    @property
    def missing_text(self) -> str:
        return "".join(self.missing_chars)


# --------------------------------------------------------------------------
# 字体解析
# --------------------------------------------------------------------------
def _fontkit():
    try:
        from . import fontkit
    except Exception as exc:  # pragma: no cover - 取决于可选模块
        raise RuntimeError(
            "字体模块 fontkit 不可用，无法生成 PDF："
            f"{exc}。请确认 galtext/fontkit.py 存在。"
        ) from exc
    return fontkit


#: 枚举中文字体要读一遍整个字体目录（约 1 秒），缓存起来避免每次导出都重扫
_CJK_FONT_CACHE: list[dict] | None = None


def cjk_fonts(refresh: bool = False) -> list[dict]:
    """列出系统中覆盖中文的字体（带缓存）。"""
    global _CJK_FONT_CACHE
    if _CJK_FONT_CACHE is None or refresh:
        _CJK_FONT_CACHE = _fontkit().find_cjk_fonts()
    return _CJK_FONT_CACHE


def pick_font(options: ScriptPdfOptions) -> tuple[object, str, str]:
    """返回 ``(字体对象, 显示名, 路径)``。

    指定了 ``font_path`` 就用它，否则按 :data:`FONT_PREFERENCE` 挑一个系统中文字体。
    """
    fontkit = _fontkit()

    if options.font_path:
        path = pathlib.Path(options.font_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到字体文件：{path}")
        font = fontkit.load_font(str(path), options.font_face_index)
        return font, str(getattr(font, "family", "") or path.stem), str(path)

    candidates = cjk_fonts()
    if not candidates:
        raise RuntimeError(
            "系统里没找到可用的中文字体，无法生成 PDF。"
            "可以用 font_path 指定一个 .ttf/.ttc 文件。"
        )

    by_name = {pathlib.Path(c["path"]).name.lower(): c for c in candidates}
    chosen = None
    for wanted in FONT_PREFERENCE:
        if wanted in by_name:
            chosen = by_name[wanted]
            break
    if chosen is None:
        chosen = candidates[0]

    font = fontkit.load_font(chosen["path"], int(chosen.get("face_index", 0)))
    family = str(getattr(font, "family", "") or chosen.get("name") or "")
    return font, family, str(chosen["path"])


# --------------------------------------------------------------------------
# 换行
# --------------------------------------------------------------------------
def wrap_text(text: str, font, size: float, max_width: float) -> list[str]:
    """按宽度折行，遵守中文禁则并保护英文单词。"""
    text = text.strip()
    if not text:
        return []
    if max_width <= 0:
        return [text]

    lines: list[str] = []
    buf: list[str] = []
    width = 0.0
    for ch in text:
        w = font.text_width(ch, size)
        if buf and width + w > max_width:
            cut = len(buf)
            # 标点不能落在行首 -> 把前一个字符一起带到下一行
            if ch in NO_LINE_START and cut > 1:
                cut -= 1
            # 开引号/括号不能落在行尾
            if cut > 1 and buf[cut - 1] in NO_LINE_END:
                cut -= 1
            # 不要从英文单词中间断开
            if (
                cut > 1
                and ch.isascii()
                and ch.isalnum()
                and buf[cut - 1].isascii()
                and buf[cut - 1].isalnum()
            ):
                for back in range(cut - 1, 0, -1):
                    if buf[back - 1] in " \t":
                        cut = back
                        break

            head = "".join(buf[:cut]).rstrip()
            carry = buf[cut:]
            if head:
                lines.append(head)
            buf = carry
            width = font.text_width("".join(buf), size)

        buf.append(ch)
        width += w

    tail = "".join(buf).rstrip()
    if tail:
        lines.append(tail)
    return lines or [text]


# --------------------------------------------------------------------------
# 场景分组
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Scene:
    index: int
    source: str
    lines: list[TextLine]

    @property
    def label(self) -> str:
        stem = pathlib.PurePosixPath(self.source.replace("\\", "/")).name
        return stem or self.source or "未命名"


def group_scenes(lines: Sequence[TextLine]) -> list[Scene]:
    """把连续的、来自同一脚本的台词归为一个场景。"""
    scenes: list[Scene] = []
    current_source: str | None = None
    bucket: list[TextLine] = []

    def flush() -> None:
        nonlocal bucket, current_source
        if bucket:
            scenes.append(Scene(len(scenes) + 1, current_source or "", bucket))
            bucket = []

    for line in lines:
        if current_source is None:
            current_source = line.source
        elif line.source != current_source:
            flush()
            current_source = line.source
        bucket.append(line)
    flush()
    return scenes


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _meta_text(options: ScriptPdfOptions, result: ScanResult | None, count: int) -> str:
    bits: list[str] = []
    if result is not None and result.engines:
        from . import parsers

        names = "、".join(parsers.engine_name(key) for key, _score in result.engines[:3])
        bits.append(f"引擎：{names}")
    if result is not None and result.scripts:
        bits.append(f"脚本 {len(result.scripts)} 个")
    bits.append(f"台词 {count} 条")
    if options.subtitle:
        bits.insert(0, options.subtitle)
    bits.append(_dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    return " ｜ ".join(bits)


def _collect_chars(
    scenes: Sequence[Scene],
    options: ScriptPdfOptions,
    title: str,
    meta: str,
) -> set[str]:
    chars: set[str] = set(title) | set(meta) | set(options.title) | set(options.subtitle)
    # 页码与小节标题要用到的固定字符，提前并进来
    chars |= set("0123456789/—–-· 　")
    chars |= set("场景第节")
    if options.show_source_path:
        chars |= set("来源")
    for scene in scenes:
        chars |= set(scene.label)
        chars |= set(scene.source)
        chars |= set(f"场景 {scene.index} · ")
        for line in scene.lines:
            chars |= set(line.speaker)
            chars |= set(line.text)
    chars.discard("\x00")
    return chars


def build_pdf(
    lines: Sequence[TextLine],
    out_path: pathlib.Path | str,
    options: ScriptPdfOptions | None = None,
    result: ScanResult | None = None,
) -> PdfResult:
    """把 ``lines`` 排成剧本 PDF 并写入 ``out_path``。"""
    options = options or ScriptPdfOptions()
    target = pathlib.Path(out_path)

    if not lines:
        raise ValueError("没有可排版的文本。")

    font, font_name, font_path = pick_font(options)
    try:
        return _build(lines, target, options, result, font, font_name, font_path)
    finally:
        close = getattr(font, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover
                pass


def _build(
    lines: Sequence[TextLine],
    target: pathlib.Path,
    options: ScriptPdfOptions,
    result: ScanResult | None,
    font,
    font_name: str,
    font_path: str,
) -> PdfResult:
    scenes = group_scenes(lines)
    title = options.title or (
        pathlib.Path(result.root).name if result is not None and result.root else "剧本"
    )
    meta = _meta_text(options, result, len(lines))
    chars = _collect_chars(scenes, options, title, meta)

    document = pdfgen.PdfDocument(
        font,
        chars,
        title=title,
        creator="GalText Extractor · 剧本导出",
    )

    width, height = document.width, document.height
    content_left = options.margin_left
    content_right = width - options.margin_right
    content_width = max(50.0, content_right - content_left)
    body_lh = options.body_size * options.line_spacing
    indent = options.body_size * options.indent_em

    page = document.new_page()
    y = height - options.margin_top

    def new_page() -> None:
        nonlocal page, y
        page = document.new_page()
        y = height - options.margin_top

    def ensure(space: float) -> None:
        nonlocal y
        if y - space < options.margin_bottom:
            new_page()

    # ---- 标题块 ----
    if options.show_title_block:
        page.text(content_left, y, title, options.heading_size * 1.5, options.heading_color)
        y -= options.heading_size * 1.5 * 1.5
        for piece in wrap_text(meta, document.font, options.meta_size, content_width):
            page.text(content_left, y, piece, options.meta_size, options.meta_color)
            y -= options.meta_size * 1.55
        y -= 4
        page.rule(content_left, y, content_right, y, 0.9, options.rule_color)
        y -= options.heading_size * 1.2

    # ---- 逐场景排版 ----
    for scene in scenes:
        if options.show_scene_headings:
            if options.scene_per_page and y < height - options.margin_top - 1:
                new_page()
            ensure(options.heading_size * 2.4 + body_lh * 2)
            if y < height - options.margin_top - 1:
                y -= options.scene_gap * options.body_size * 0.5
            heading = f"场景 {scene.index} · {scene.label}"
            page.text(content_left, y, heading, options.heading_size, options.heading_color)
            y -= options.heading_size * 1.5
            if options.show_source_path and scene.source and scene.source != scene.label:
                page.text(
                    content_left, y, scene.source, options.meta_size, options.meta_color
                )
                y -= options.meta_size * 1.7
            page.rule(content_left, y, content_right, y, 0.6, options.rule_color)
            y -= options.body_size * 1.15

        for line in scene.lines:
            wrapped = wrap_text(line.text, document.font, options.body_size, content_width - indent)
            if not wrapped:
                continue

            if line.speaker:
                # 角色名不能单独留在页尾：至少要能容下角色名 + 首行台词
                ensure(options.speaker_size * 1.55 + body_lh)
                page.text(
                    content_left, y, line.speaker, options.speaker_size, options.speaker_color
                )
                y -= options.speaker_size * 1.55

            color = options.text_color if line.speaker else options.narration_color
            for piece in wrapped:
                if y < options.margin_bottom + body_lh:
                    new_page()
                    if line.speaker:
                        page.text(
                            content_left,
                            y,
                            line.speaker,
                            options.speaker_size,
                            options.speaker_color,
                        )
                        y -= options.speaker_size * 1.55
                page.text(content_left + indent, y, piece, options.body_size, color)
                y -= body_lh

            y -= options.body_size * options.block_gap

    # ---- 页脚 ----
    total = len(document.pages)
    if options.show_footer:
        for number, (_obj, page_obj) in enumerate(document.pages, 1):
            footer_y = options.margin_bottom * 0.46
            page_obj.rule(
                content_left,
                options.margin_bottom * 0.76,
                content_right,
                options.margin_bottom * 0.76,
                0.5,
                options.rule_color,
            )
            page_obj.text(
                content_left, footer_y, title, options.footer_size, options.meta_color
            )
            page_obj.text_centered(
                footer_y,
                f"— {number} / {total} —",
                options.footer_size,
                options.meta_color,
                content_left,
                content_right,
            )

    document.save(target)

    missing = sorted(document.font.missing)
    if missing:
        log.warning("字体缺少 %d 个字符，PDF 中会缺失：%s", len(missing), "".join(missing[:40]))
    log.info(
        "剧本 PDF：%d 页 / %d 场景 / %d 条台词 / %s",
        total,
        len(scenes),
        len(lines),
        target,
    )
    return PdfResult(
        path=target,
        pages=total,
        lines=len(lines),
        scenes=len(scenes),
        missing_chars=missing,
        font_name=font_name,
        font_path=font_path,
        size_bytes=target.stat().st_size,
    )


def available_fonts() -> list[dict]:
    """给界面用的字体列表；失败时返回空列表而不是抛异常。"""
    try:
        return cjk_fonts()
    except Exception as exc:
        log.warning("枚举字体失败：%s", exc)
        return []

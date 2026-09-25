"""命令行界面。

::

    python -m galtext engines                 # 看有哪些解析器可用
    python -m galtext scan "D:\\Game" -o out.csv -f csv
    python -m galtext scan "D:\\Game" -f pdf -o script.pdf
    python -m galtext scan "D:\\Game" --include-generic --aggressive
    python -m galtext gui                     # 打开图形界面
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

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
from .common import human_size


def _fix_console() -> None:
    """Windows 控制台默认是 GBK，直接 print 日文会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="galtext",
        description=f"{__app_name__} —— 从 galgame 目录提取对话文本（作者：{__author__}）",
        epilog=(
            "示例：\n"
            "  galtext engines\n"
            '  galtext scan "D:\\Game" -o out.csv -f csv\n'
            '  galtext scan "D:\\Game" -f pdf -o script.pdf\n'
            "  galtext gui\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}  |  {__author__}  |  {__license__}",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command")

    # --- scan ---
    scan = sub.add_parser("scan", help="扫描游戏目录并导出文本")
    scan.add_argument("root", nargs="?", default="", help="游戏目录（--pdf-list-fonts 时可省略）")
    scan.add_argument("-o", "--output", default="", help="输出文件路径（默认按目录名生成）")
    scan.add_argument(
        "-f",
        "--format",
        default="txt",
        choices=list(exporter.ALL_FORMATS),
        help="导出格式（默认 txt；pdf 为剧本样式）",
    )
    scan.add_argument(
        "-e",
        "--encoding",
        default=exporter.DEFAULT_ENCODING,
        choices=list(exporter.ENCODINGS),
        help="输出文件编码（默认 utf-8-sig）",
    )
    scan.add_argument(
        "--force-encoding",
        default="auto",
        help="强制按指定编码解读脚本（默认 auto 自动探测）",
    )
    scan.add_argument(
        "--engines",
        default="",
        help="只使用指定解析器，逗号分隔（如 kirikiri,nscripter）",
    )
    scan.add_argument(
        "--include-generic",
        action="store_true",
        help="启用通用二进制扫描（慢，准确性低）",
    )
    scan.add_argument("--no-dedupe", action="store_true", help="不去重")
    scan.add_argument("--min-len", type=int, default=2, help="最短文本长度（默认 2）")
    scan.add_argument("--max-len", type=int, default=400, help="最长文本长度（默认 400）")
    scan.add_argument(
        "--aggressive",
        action="store_true",
        help="激进模式：放宽代码行过滤，宁可多收",
    )
    scan.add_argument(
        "--keep-ascii",
        action="store_true",
        help="保留纯 ASCII 行（默认丢弃）",
    )
    scan.add_argument(
        "--allow-no-japanese",
        action="store_true",
        help="不要求文本含日文/汉字",
    )
    scan.add_argument(
        "--chinese-only",
        action="store_true",
        help="只保留中文行（丢掉含假名的日文行）—— 用于原版与汉化补丁并存的游戏",
    )
    scan.add_argument(
        "--narration-label",
        default="",
        help="给旁白补的说话人名字（例如填 旁白），让每行都有名字",
    )
    scan.add_argument(
        "--guess-speakers",
        action="store_true",
        help="把单独成行的短行当成角色名（名字与台词分行的脚本；启发式，可能误判）",
    )
    scan.add_argument(
        "--no-speaker-tracking",
        action="store_true",
        help="不跨行跟踪 [name text=...] 这类说话人声明",
    )
    scan.add_argument(
        "--keep-all-versions",
        action="store_true",
        help="关闭封包覆盖语义：同一脚本在多个封包里各有一份时全部保留（用于对照原文与译文）",
    )
    scan.add_argument("--no-tags", action="store_true", help="不剥离 [tag] 之类的标记")
    scan.add_argument(
        "--no-speaker",
        action="store_true",
        help="txt 格式不写说话人",
    )
    scan.add_argument("--quiet", action="store_true", help="安静模式，只输出结果路径")

    # --- PDF 专用 ---
    pdf = scan.add_argument_group("剧本 PDF 选项（-f pdf 时生效）")
    pdf.add_argument("--pdf-title", default="", help="标题（默认用目录名）")
    pdf.add_argument(
        "--pdf-font",
        default="",
        help="字体文件 .ttf/.ttc（默认自动挑一个系统中文字体）",
    )
    pdf.add_argument(
        "--pdf-font-index", type=int, default=0, help="字体集合(.ttc)里的第几个字面，默认 0"
    )
    pdf.add_argument(
        "--pdf-scene-per-page", action="store_true", help="每个场景另起一页"
    )
    pdf.add_argument("--pdf-flat", action="store_true", help="不显示「场景 N · 文件名」标题")
    pdf.add_argument("--pdf-no-source", action="store_true", help="不显示脚本来源路径")
    pdf.add_argument("--pdf-list-fonts", action="store_true", help="列出可用中文字体后退出")

    # --- engines ---
    sub.add_parser("engines", help="列出可用解析器")

    # --- gui ---
    gui = sub.add_parser("gui", help="打开图形界面")
    gui.add_argument("root", nargs="?", default="", help="可选：启动时直接扫描的目录")

    return parser


def _cmd_engines() -> int:
    print(f"{__app_name__} {__version__}  ·  {__author__}  ·  {__license__}")
    print()
    print(f"{'关键字':<12}{'名称':<32}{'状态'}")
    print("-" * 60)
    for info in parsers.list_engines():
        status = "可用" if info["available"] else f"不可用（{info['error']}）"
        print(f"{info['key']:<12}{info['name']:<32}{status}")
    print()
    print("提示：用 --engines 关键字列表 可以只跑其中几个。")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.pdf_list_fonts:
        fonts = scriptpdf.available_fonts()
        if not fonts:
            print("没有找到可用的中文字体。", file=sys.stderr)
            return 2
        print(f"{'名称':<34}{'文件':<28}{'字面':>5}")
        print("-" * 70)
        for info in fonts:
            print(
                f"{str(info.get('name', ''))[:32]:<34}"
                f"{pathlib.Path(info['path']).name[:26]:<28}"
                f"{info.get('face_index', 0):>5}"
            )
        print()
        print("用 --pdf-font <路径> --pdf-font-index <字面> 指定其中一个。")
        return 0

    if not args.root:
        print("请指定游戏目录。用 -h 查看用法。", file=sys.stderr)
        return 2

    clean = textkit.CleanOptions(
        min_len=args.min_len,
        max_len=args.max_len,
        require_japanese=not args.allow_no_japanese,
        drop_ascii_only=not args.keep_ascii,
        aggressive=args.aggressive,
        strip_tags=not args.no_tags,
        chinese_only=args.chinese_only,
        track_speakers=not args.no_speaker_tracking,
        guess_bare_speakers=args.guess_speakers,
        narration_speaker=args.narration_label,
    )
    engines = [e.strip() for e in args.engines.split(",") if e.strip()] or None
    options = pipeline.ExtractOptions(
        engines=engines,
        include_generic=args.include_generic,
        encoding=args.force_encoding,
        clean=clean,
        dedupe=not args.no_dedupe,
        override_archives=not args.keep_all_versions,
    )

    last_percent = -1

    def on_progress(percent: float | None, message: str) -> None:
        nonlocal last_percent
        if args.quiet:
            return
        if percent is None:
            print(message)
            return
        bucket = int(percent * 20)
        if bucket != last_percent:
            last_percent = bucket
            print(f"[{'#' * bucket}{'.' * (20 - bucket)}] {percent * 100:5.1f}%  {message}")

    root = pathlib.Path(args.root)
    result = pipeline.scan(root, options, progress=on_progress)

    if result.cancelled:
        print("已取消。", file=sys.stderr)
        return 130
    for warning in result.warnings:
        print(f"注意：{warning}", file=sys.stderr)

    if not result.lines:
        print("没有提取到任何文本。", file=sys.stderr)
        return 2

    if args.output:
        out_path = pathlib.Path(args.output)
    else:
        stem = root.name or "galtext"
        out_path = pathlib.Path.cwd() / f"{stem}{exporter.EXTENSIONS[args.format]}"

    # ---- 剧本 PDF 走单独的排版引擎 ----
    if args.format == exporter.PDF_FORMAT:
        pdf_options = scriptpdf.ScriptPdfOptions(
            title=args.pdf_title,
            font_path=args.pdf_font,
            font_face_index=args.pdf_font_index,
            scene_per_page=args.pdf_scene_per_page,
            show_scene_headings=not args.pdf_flat,
            show_source_path=not args.pdf_no_source,
        )
        try:
            pdf_result = exporter.export_pdf(
                out_path, result.lines, options=pdf_options, result=result
            )
        except Exception as exc:
            print(f"生成 PDF 失败：{exc}", file=sys.stderr)
            return 3

        if args.quiet:
            print(pdf_result.path)
        else:
            print()
            print(pipeline.format_summary(result))
            print(f"已导出：{pdf_result.path}")
            print(
                f"  {pdf_result.pages} 页 / {pdf_result.scenes} 个场景 / "
                f"{pdf_result.lines} 条台词 / {human_size(pdf_result.size_bytes)}"
            )
            print(f"  字体：{pdf_result.font_name}（{pathlib.Path(pdf_result.font_path).name}）")
        if pdf_result.missing_chars:
            preview = "".join(pdf_result.missing_chars[:30])
            print(
                f"注意：字体缺少 {len(pdf_result.missing_chars)} 个字符，"
                f"PDF 中未渲染：{preview}",
                file=sys.stderr,
            )
        return 0

    exporter.export(
        out_path,
        result.lines,
        fmt=args.format,
        encoding=args.encoding,
        with_speaker=not args.no_speaker,
        result=result,
    )

    if args.quiet:
        print(out_path)
    else:
        print()
        print(pipeline.format_summary(result))
        print(f"已导出：{out_path}")
    return 0


def _cmd_gui(args: argparse.Namespace) -> int:
    from . import gui

    return gui.main(args.root or None)


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "engines":
        return _cmd_engines()
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "gui":
        return _cmd_gui(args)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

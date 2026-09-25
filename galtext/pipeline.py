"""扫描调度：引擎探测 -> 脚本发现 -> 文本抽取 -> 汇总。

对 GUI 与 CLI 都只暴露 ``scan()`` 一个入口：

>>> result = scan(Path("D:/Game"), ExtractOptions())
>>> len(result.lines)
12345
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import time
from typing import Callable, Iterable

from . import parsers, textkit
from .common import RawScript, ScanResult, ScriptReport, TextLine, human_size, safe_relative

log = logging.getLogger(__name__)

ProgressFn = Callable[[float | None, str], None]
CancelFn = Callable[[], bool]


@dataclasses.dataclass
class ExtractOptions:
    """一次扫描的全部可调参数。"""

    engines: list[str] | None = None
    """指定使用哪些解析器；``None`` 表示自动挑选。"""

    include_generic: bool = False
    """是否启用通用二进制扫描（慢，但几乎什么都能捞一点）。"""

    auto_generic_if_empty: bool = True
    """其它解析器一无所获时，自动退化为通用扫描。"""

    encoding: str = "auto"
    """强制编码（``auto`` 为自动探测）。"""

    clean: textkit.CleanOptions = dataclasses.field(default_factory=textkit.CleanOptions)
    """文本精度过滤选项。"""

    min_engine_score: int = 1
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    generic_max_file_bytes: int = 16 * 1024 * 1024
    generic_max_total_bytes: int = 256 * 1024 * 1024
    dedupe: bool = True

    override_archives: bool = True
    """启用封包覆盖语义：同一脚本在多个封包中各有一份时，只保留优先级最高的。

    ``data.xp3``（原版）+ ``patch.xp3``（汉化）这种布局下，引擎只会用到补丁那份；
    关掉这个选项会把两版都提取出来，方便自己对比原文与译文。
    """

    def clone(self, **kwargs) -> "ExtractOptions":
        data = {
            "engines": self.engines,
            "include_generic": self.include_generic,
            "auto_generic_if_empty": self.auto_generic_if_empty,
            "encoding": self.encoding,
            "clean": self.clean.clone(),
            "min_engine_score": self.min_engine_score,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "generic_max_file_bytes": self.generic_max_file_bytes,
            "generic_max_total_bytes": self.generic_max_total_bytes,
            "dedupe": self.dedupe,
            "override_archives": self.override_archives,
        }
        data.update(kwargs)
        return ExtractOptions(**data)


def _noop_progress(_percent: float | None, _message: str) -> None:
    return


def detect_engines(root: pathlib.Path) -> list[tuple[str, int]]:
    """返回 ``[(engine_key, score), ...]``，按置信度降序。"""
    return parsers.detect(root)


def _select_engines(
    scores: list[tuple[str, int]], options: ExtractOptions
) -> list[str]:
    """决定这次要跑哪些解析器。"""
    available = {e["key"]: e for e in parsers.list_engines() if e["available"]}
    if options.engines:
        # 用户显式指定了就完全照办，不要再自作主张加别的解析器
        return [e for e in options.engines if e in available]

    chosen = [
        key
        for key, score in scores
        if score >= options.min_engine_score and key in available
    ]
    # 自动模式下总是加上 plaintext：硬盘版/汉化版经常把脚本散放
    if "plaintext" in available and "plaintext" not in chosen:
        chosen.append("plaintext")
    # 用户勾了「通用二进制扫描」就一定要跑
    if options.include_generic and "generic" in available and "generic" not in chosen:
        chosen.append("generic")
    if not chosen:
        chosen = [k for k in ("plaintext",) if k in available]
    return chosen


#: 当成「封包」的扩展名。虚拟路径的第一段若以这些结尾，就认为它是封包名。
ARCHIVE_SUFFIXES = frozenset(
    {".xp3", ".nsa", ".sar", ".arc", ".ypf", ".pfs", ".pak", ".g00"}
)


def split_virtual_path(virtual_path: str) -> tuple[str, str]:
    """``data.xp3/scenario/foo.ks`` -> ``("data.xp3", "scenario/foo.ks")``。

    散装文件（``scenario/foo.ks``、``nscript.dat``）返回 ``("", 原路径)``，
    也就是「不属于任何封包」。
    """
    head, sep, rest = virtual_path.partition("/")
    if sep and pathlib.PurePosixPath(head).suffix.lower() in ARCHIVE_SUFFIXES:
        return head, rest
    return "", virtual_path


def override_rank(script: RawScript) -> tuple:
    """覆盖优先级，**越大越优先**。

    * 散装文件最高 —— 引擎优先读封包外的同名文件，汉化补丁常这么放；
    * 封包之间按封包名比较，**名字靠后的覆盖靠前的**
      （KiriKiri 按文件名顺序加载，后加载的盖掉先加载的，
      所以 ``patch.xp3`` 会盖掉 ``data.xp3``）；
    * 最后用虚拟路径兜底，保证排序稳定、结果可复现。
    """
    container, inner = split_virtual_path(script.virtual_path)
    if not container:
        return (1, "", inner.lower())
    return (0, container.lower(), inner.lower())


def resolve_overrides(
    scripts: "list[RawScript]",
) -> tuple[list[RawScript], list[tuple[RawScript, RawScript]]]:
    """按覆盖关系去重。

    返回 ``(保留的脚本, [(被覆盖的脚本, 覆盖它的脚本), ...])``。
    被覆盖的不会消失 —— 上层会把它们列进报告，让「跳过了什么」是可见的。
    """
    best: dict[str, RawScript] = {}
    for script in scripts:
        _container, inner = split_virtual_path(script.virtual_path)
        key = inner.lower()
        current = best.get(key)
        if current is None or override_rank(script) > override_rank(current):
            best[key] = script

    kept: list[RawScript] = []
    overridden: list[tuple[RawScript, RawScript]] = []
    for script in scripts:
        _container, inner = split_virtual_path(script.virtual_path)
        winner = best[inner.lower()]
        if winner is script:
            kept.append(script)
        else:
            overridden.append((script, winner))
    return kept, overridden


def scan(
    root: pathlib.Path | str,
    options: ExtractOptions | None = None,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> ScanResult:
    """扫描一个游戏目录并抽取文本。"""
    options = options or ExtractOptions()
    report = progress or _noop_progress
    cancelled = should_cancel or (lambda: False)
    started = time.time()

    root_path = pathlib.Path(root).expanduser()
    result = ScanResult(root=str(root_path))
    if not root_path.is_dir():
        result.warnings.append(f"不是有效的目录：{root_path}")
        return result

    # ---- 1. 引擎探测 --------------------------------------------------
    report(0.02, "正在识别游戏引擎…")
    scores = detect_engines(root_path)
    result.engines = scores
    for key, score in scores:
        log.info("检测到引擎 %s（置信度 %d）", parsers.engine_name(key), score)
    if cancelled():
        result.cancelled = True
        return result

    target_engines = _select_engines(scores, options)
    if not target_engines:
        result.warnings.append("没有可用的解析器。")
        return result
    log.info("本次使用解析器：%s", ", ".join(target_engines))

    # ---- 2. 发现脚本 --------------------------------------------------
    report(0.05, "正在扫描脚本文件…")
    candidates: list[RawScript] = []
    total_bytes = 0
    covered_virtual: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    truncated = False

    for index, engine in enumerate(target_engines):
        if cancelled():
            result.cancelled = True
            return result
        base = 0.05 + 0.35 * (index / max(1, len(target_engines)))
        report(base, f"正在扫描：{parsers.engine_name(engine)}")

        kwargs: dict[str, object] = {}
        if engine == "generic":
            kwargs["max_file_bytes"] = options.generic_max_file_bytes
            kwargs["max_total_bytes"] = options.generic_max_total_bytes
        elif engine == "plaintext":
            kwargs["max_file_bytes"] = options.max_file_bytes

        count_for_engine = 0
        for virtual_path, data, container in parsers.iter_scripts(engine, root_path, **kwargs):
            if cancelled():
                result.cancelled = True
                return result
            # plaintext 跳过已经被专业解析器收走的散装文件
            if engine == "plaintext" and virtual_path in covered_virtual:
                continue
            key = (container, virtual_path)
            if key in seen_keys:
                continue
            if len(data) > options.max_file_bytes:
                continue
            if total_bytes + len(data) > options.max_total_bytes:
                if not truncated:
                    result.warnings.append(
                        f"脚本总量超过 {human_size(options.max_total_bytes)}，"
                        "后续文件已跳过（可以在设置里调大上限）。"
                    )
                    truncated = True
                continue
            total_bytes += len(data)
            seen_keys.add(key)
            candidates.append(
                RawScript(
                    engine=engine,
                    virtual_path=virtual_path,
                    data=data,
                    origin=container or str(root_path),
                    container="" if container == str(root_path) else container,
                )
            )
            if engine != "plaintext":
                covered_virtual.add(virtual_path)
            count_for_engine += 1

        if count_for_engine:
            log.info("%s：发现 %d 个脚本", parsers.engine_name(engine), count_for_engine)
        if engine == "plaintext":
            # 专业解析器可能把散装文件也报了一遍，这里补一次去重
            covered_virtual.update(s.virtual_path for s in candidates if s.engine != "plaintext")

    # ---- 2b. 解析覆盖关系 ---------------------------------------------
    # 「原版 + 汉化补丁」并存的游戏里，同一个脚本会在多个封包中各有一份
    # （例如 data.xp3 是日文、patch.xp3 是中文）。引擎的语义是**后加载的覆盖先加载的**，
    # 所以我们只该保留优先级最高的那一份 —— 否则原文和汉化会一起被提出来。
    if options.override_archives:
        raw_scripts, overridden = resolve_overrides(candidates)
    else:
        raw_scripts, overridden = list(candidates), []
    if overridden:
        log.info("有 %d 个脚本被更高优先级的版本覆盖，已跳过", len(overridden))

    # 一无所获时自动兜底
    if not raw_scripts and options.auto_generic_if_empty and not options.include_generic:
        log.info("常规解析没有结果，自动启用通用二进制扫描")
        result.warnings.append("常规解析未发现脚本，已自动改用通用二进制扫描（结果准确性较低）。")
        for virtual_path, data, container in parsers.iter_scripts(
            "generic",
            root_path,
            max_file_bytes=options.generic_max_file_bytes,
            max_total_bytes=options.generic_max_total_bytes,
        ):
            if cancelled():
                result.cancelled = True
                return result
            raw_scripts.append(
                RawScript(engine="generic", virtual_path=virtual_path, data=data, origin=str(root_path))
            )
        overridden = []

    if not raw_scripts:
        result.warnings.append("没有找到任何可解析的脚本文件。")
        report(1.0, "完成：没有找到文本")
        return result

    log.info(
        "共保留 %d 个脚本文本，合计 %s",
        len(raw_scripts),
        human_size(sum(s.size for s in raw_scripts)),
    )

    # ---- 3. 抽取文本 --------------------------------------------------
    total = len(raw_scripts)
    seen_lines: set[tuple[str, str]] = set()
    out_index = 0
    for position, script in enumerate(raw_scripts):
        if cancelled():
            result.cancelled = True
            return result
        if position % 5 == 0 or position == total - 1:
            report(
                0.40 + 0.58 * (position / total),
                f"正在提取文本（{position + 1}/{total}）：{script.virtual_path}",
            )

        module = parsers.pick_extractor(script.engine, script.virtual_path)
        encoding_name = ""
        if options.encoding and options.encoding.lower() != "auto":
            encoding_name = options.encoding
        elif module is not None:
            # 解析器自己最清楚它用什么编码解开的（比如 nscript.dat 要先 XOR）
            guess = getattr(module, "guess_encoding", None)
            if guess is not None:
                try:
                    encoding_name = str(guess(script.virtual_path, script.data))
                except Exception:  # pragma: no cover
                    encoding_name = ""
        if not encoding_name:
            try:
                encoding_name = textkit.decode_auto(script.data[:65536])[1]
            except Exception:  # pragma: no cover
                encoding_name = "?"

        rows: list[tuple[str, str]] = []
        error = ""
        if module is None:
            error = "没有可用的解析器"
        else:
            try:
                rows = parsers.call_extract_lines(
                    module, script.virtual_path, script.data, options.clean, options.encoding
                )
            except Exception as exc:  # pragma: no cover - 兜底
                log.warning("解析 %s 失败：%s", script.virtual_path, exc)
                error = str(exc)

        report_item = ScriptReport(
            virtual_path=script.virtual_path,
            engine=script.engine,
            origin=safe_relative(pathlib.Path(script.origin), root_path)
            if script.origin
            else "",
            container=script.container,
            size=script.size,
            line_count=len(rows),
            encoding=encoding_name,
            error=error,
        )
        result.scripts.append(report_item)

        for speaker, text in rows:
            if options.dedupe:
                key = (speaker, text)
                if key in seen_lines:
                    continue
                seen_lines.add(key)
            out_index += 1
            result.lines.append(
                TextLine(
                    text=text,
                    speaker=speaker,
                    source=script.virtual_path,
                    engine=script.engine,
                    index=out_index,
                    container=script.container,
                )
            )

    # 被覆盖的脚本也要列出来 —— 让「跳过了什么、为什么」是可见的，
    # 而不是让人以为工具漏读了汉化补丁。
    for script, winner in overridden:
        result.scripts.append(
            ScriptReport(
                virtual_path=script.virtual_path,
                engine=script.engine,
                origin=safe_relative(pathlib.Path(script.origin), root_path)
                if script.origin
                else "",
                container=script.container,
                size=script.size,
                line_count=0,
                encoding="",
                note=f"被 {winner.virtual_path} 覆盖，已跳过（引擎只会加载后者）",
            )
        )
    if overridden:
        result.warnings.append(
            f"有 {len(overridden)} 个脚本被更高优先级的版本覆盖"
            "（例如 patch.xp3 盖掉 data.xp3），已按引擎语义跳过。"
            "想同时提取原版与汉化两版做对照，请关闭「封包覆盖」。"
        )

    # 让「还残留日文」这件事可见：数一数有多少行带假名（中文行基本不含假名）。
    # 补丁没覆盖到的文件、以及补丁里没翻译的句子，都会落在这里。
    if not options.clean.chinese_only:
        kana_lines = sum(1 for line in result.lines if textkit.has_kana(line.text))
        if kana_lines:
            ratio = kana_lines / max(1, len(result.lines))
            result.warnings.append(
                f"有 {kana_lines} 条文本含假名（占 {ratio:.0%}），"
                "通常是补丁未覆盖的部分或未翻译的句子。"
                "想要纯中文输出，请打开「只保留中文行」。"
            )

    elapsed = time.time() - started
    report(1.0, f"完成：{len(result.lines)} 条文本，用时 {elapsed:.1f} 秒")
    log.info(
        "扫描完成：%d 个脚本 / %d 条文本 / %.1f 秒",
        len(result.scripts),
        len(result.lines),
        elapsed,
    )
    return result


def format_summary(result: ScanResult) -> str:
    """给 CLI / 界面用的一行摘要。"""
    if not result.engines:
        engine_text = "未识别出已知引擎"
    else:
        engine_text = "、".join(
            f"{parsers.engine_name(key)}（{score}）" for key, score in result.engines[:3]
        )
    scripts_with_text = sum(1 for s in result.scripts if s.line_count)
    return (
        f"引擎：{engine_text}｜脚本 {len(result.scripts)} 个"
        f"（其中 {scripts_with_text} 个含文本）｜文本 {len(result.lines)} 条"
    )


def engine_choices() -> list[tuple[str, str]]:
    """给 GUI 用的 ``[(key, 显示名)]`` 列表。"""
    return [
        (info["key"], info["name"])
        for info in parsers.list_engines()
        if info["available"]
    ]

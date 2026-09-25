"""导出器：把抽取结果写成各种格式。

支持：

====== ==========================================================
``txt``      纯文本，一行一句，可带说话人
``csv``      表格（序号 / 说话人 / 文本 / 来源 / 引擎）
``tsv``      同 CSV，制表符分隔
``json``     完整结构化结果（含文件清单与引擎信息）
``jsonl``    一行一个 JSON 对象，方便流式处理
``template`` 翻译模板：``原文`` 一列填好，``译文`` 留空
====== ==========================================================
"""

from __future__ import annotations

import csv
import json
import pathlib
from typing import Iterable, Sequence

from .common import ScanResult, TextLine

FORMATS: tuple[str, ...] = ("txt", "csv", "tsv", "json", "jsonl", "template")

#: 剧本 PDF 走单独的排版引擎（:mod:`galtext.scriptpdf`），
#: 但对外仍然从 :func:`export` 统一入口进入。
PDF_FORMAT = "pdf"
ALL_FORMATS: tuple[str, ...] = FORMATS + (PDF_FORMAT,)

FORMAT_LABELS: dict[str, str] = {
    "txt": "纯文本 (.txt)",
    "csv": "表格 CSV (.csv)",
    "tsv": "表格 TSV (.tsv)",
    "json": "结构化 JSON (.json)",
    "jsonl": "JSON Lines (.jsonl)",
    "template": "翻译模板 (.csv)",
    "pdf": "剧本 PDF (.pdf)",
}

#: 常用编码（Excel / 记事本 / 各汉化工具的口味不同）
ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "utf-16", "cp932", "cp936", "cp950")

DEFAULT_ENCODING = "utf-8-sig"

EXTENSIONS: dict[str, str] = {
    "txt": ".txt",
    "csv": ".csv",
    "tsv": ".tsv",
    "json": ".json",
    "jsonl": ".jsonl",
    "template": ".csv",
    "pdf": ".pdf",
}


def _rows(lines: Iterable[TextLine]) -> list[TextLine]:
    return list(lines)


def write_txt(
    path: pathlib.Path,
    lines: Sequence[TextLine],
    encoding: str = DEFAULT_ENCODING,
    with_speaker: bool = True,
    header: str = "",
) -> pathlib.Path:
    """一行一句。默认只写台词，不带来源等元信息，方便直接拿去翻译。"""
    with path.open("w", encoding=encoding, newline="\n") as fp:
        if header:
            fp.write(header.rstrip("\n") + "\n\n")
        for line in lines:
            fp.write((line.display() if with_speaker else line.text) + "\n")
    return path


def write_csv(
    path: pathlib.Path,
    lines: Sequence[TextLine],
    encoding: str = DEFAULT_ENCODING,
    delimiter: str = ",",
) -> pathlib.Path:
    with path.open("w", encoding=encoding, newline="") as fp:
        writer = csv.writer(fp, delimiter=delimiter)
        writer.writerow(["序号", "说话人", "文本", "来源", "引擎"])
        for line in lines:
            writer.writerow(
                [
                    line.index,
                    line.speaker,
                    line.text,
                    line.source,
                    line.engine,
                ]
            )
    return path


def write_template(
    path: pathlib.Path,
    lines: Sequence[TextLine],
    encoding: str = DEFAULT_ENCODING,
) -> pathlib.Path:
    """翻译模板：``原文`` 已填好，``译文`` 留空，另附说话人与来源便于校对。"""
    with path.open("w", encoding=encoding, newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["序号", "说话人", "原文", "译文", "来源", "引擎"])
        for line in lines:
            writer.writerow(
                [
                    line.index,
                    line.speaker,
                    line.text,
                    "",
                    line.source,
                    line.engine,
                ]
            )
    return path


def write_json(
    path: pathlib.Path,
    lines: Sequence[TextLine],
    encoding: str = DEFAULT_ENCODING,
    result: ScanResult | None = None,
) -> pathlib.Path:
    payload: dict[str, object]
    if result is not None:
        payload = result.to_dict()
    else:
        payload = {"lines": [ln.to_dict() for ln in lines]}
    with path.open("w", encoding=encoding) as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return path


def write_jsonl(
    path: pathlib.Path,
    lines: Sequence[TextLine],
    encoding: str = DEFAULT_ENCODING,
) -> pathlib.Path:
    with path.open("w", encoding=encoding, newline="\n") as fp:
        for line in lines:
            fp.write(json.dumps(line.to_dict(), ensure_ascii=False) + "\n")
    return path


def export_pdf(
    path: pathlib.Path | str,
    lines: Sequence[TextLine],
    options=None,
    result: ScanResult | None = None,
):
    """导出剧本 PDF，返回 :class:`galtext.scriptpdf.PdfResult`（含页数/缺字等信息）。"""
    from . import scriptpdf

    out = pathlib.Path(path)
    if out.is_dir():
        out = out / ("galtext" + EXTENSIONS[PDF_FORMAT])
    out.parent.mkdir(parents=True, exist_ok=True)
    return scriptpdf.build_pdf(lines, out, options=options, result=result)


def export(
    path: pathlib.Path | str,
    lines: Sequence[TextLine],
    fmt: str = "txt",
    encoding: str = DEFAULT_ENCODING,
    with_speaker: bool = True,
    header: str = "",
    result: ScanResult | None = None,
    pdf_options=None,
) -> pathlib.Path:
    """按 ``fmt`` 导出到 ``path``，返回实际写入的路径。"""
    fmt = (fmt or "txt").lower()
    if fmt not in ALL_FORMATS:
        raise ValueError(
            f"不支持的导出格式：{fmt}（可选：{', '.join(ALL_FORMATS)}）"
        )
    if fmt == PDF_FORMAT:
        return export_pdf(path, lines, options=pdf_options, result=result).path

    out = pathlib.Path(path)
    if out.is_dir():
        out = out / ("galtext" + EXTENSIONS[fmt])
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "txt":
        return write_txt(out, lines, encoding, with_speaker, header)
    if fmt == "csv":
        return write_csv(out, lines, encoding, ",")
    if fmt == "tsv":
        return write_csv(out, lines, encoding, "\t")
    if fmt == "json":
        return write_json(out, lines, encoding, result)
    if fmt == "jsonl":
        return write_jsonl(out, lines, encoding)
    if fmt == "template":
        return write_template(out, lines, encoding)
    raise ValueError(f"不支持的导出格式：{fmt}")  # pragma: no cover

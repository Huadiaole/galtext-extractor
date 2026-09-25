"""解析器注册表。

每个引擎一个模块，统一契约（见 :mod:`galtext.common`）::

    ENGINE_ID: str
    ENGINE_NAME: str
    def detect_dir(root) -> int              # 0-100 置信度
    def iter_scripts(root)                   # 产出 (virtual_path, data) 或 (vp, data, container)
    def extract_lines(virtual_path, data) -> list[str]

本模块负责：延迟导入、容错（某个引擎模块坏掉不影响其它）、
以及把各家 ``extract_lines`` 的结果统一成 ``(说话人, 台词)``。
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
import pathlib
from types import ModuleType
from typing import Any, Iterator

from .. import textkit

log = logging.getLogger(__name__)

# 专业引擎（有真实封包/字节码解析）
SPECIALIZED_ENGINES = ("kirikiri", "reallive", "nscripter", "bgi")
# 通用模块
GENERIC_ENGINES = ("plaintext", "generic")

#: 内置解析器的**尝试与展示顺序**。
#: 不在这个元组里的模块同样会被自动发现，只是排在后面 ——
#: 所以第三方解析器只要把 ``.py`` 丢进本目录就能生效，不必改这里。
PREFERRED_ORDER = SPECIALIZED_ENGINES + GENERIC_ENGINES

#: 兼容旧名字（外部代码引用的就是这个）
ALL_ENGINES = PREFERRED_ORDER

#: **有意预留、本版本尚未实现**的引擎。
#: 模块不存在时也会出现在 `galtext engines` 里并标记为「不可用」，
#: 让用户看得出这是留好的扩展位（补上同名模块即可自动生效）。
PLANNED_ENGINES = ("reallive",)

# 散装脚本按扩展名路由到对应的专业解析器
EXTENSION_ROUTES: dict[str, str] = {
    ".ks": "kirikiri",
    ".tjs": "kirikiri",
    ".asd": "kirikiri",
    ".ws2": "bgi",
    ".mes": "bgi",
    ".nscript": "nscripter",
}


@dataclasses.dataclass
class ParserInfo:
    engine_id: str
    engine_name: str
    module: ModuleType | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.module is not None


_REGISTRY: dict[str, ParserInfo] | None = None


def discover_modules() -> list[str]:
    """列出本包下所有解析器模块名（自动发现，包含用户自己加的）。

    这是「把 ``.py`` 丢进 ``parsers/`` 就能用」的实现依据：
    不依赖任何写死的清单，``pkgutil`` 扫到什么就加载什么。
    :data:`PREFERRED_ORDER` 只决定先后顺序，不影响是否被发现。

    唯一例外是 :data:`PLANNED_ENGINES` —— 那些是**有意预留、本版本还没实现**
    的引擎。模块不在磁盘上也要列出来（会显示成「不可用」），
    这样用户能看出这是留好的位置，而不是以为工具漏了什么。
    """
    try:
        import pkgutil

        found = {
            info.name
            for info in pkgutil.iter_modules(__path__)
            if not info.name.startswith("_")
        }
    except Exception as exc:  # pragma: no cover - 包结构异常时才走到
        log.warning("自动发现解析器失败，回退到内置列表：%s", exc)
        return list(PREFERRED_ORDER)

    found.discard("__init__")
    ordered = [name for name in PREFERRED_ORDER if name in found]
    extra = sorted(found - set(PREFERRED_ORDER))
    if extra:
        log.debug("发现额外的解析器模块：%s", ", ".join(extra))
    # 计划中但还没写出来的引擎照样列出来，让「不可用」这件事是可见的
    planned = [name for name in PLANNED_ENGINES if name not in found]
    return ordered + extra + planned


def _load_one(name: str) -> ParserInfo:
    try:
        module = importlib.import_module(f".{name}", __name__)
    except Exception as exc:  # pragma: no cover - 取决于可选模块
        # 用 info 级别：缺失的引擎会在 `galtext engines` 和界面的
        # 「支持的解析器」里明确列出来，不必每次运行都刷警告。
        log.info("解析器 %s 不可用：%s", name, exc)
        return ParserInfo(engine_id=name, engine_name=name, module=None, error=str(exc))
    engine_id = getattr(module, "ENGINE_ID", name)
    engine_name = getattr(module, "ENGINE_NAME", name)
    return ParserInfo(engine_id=engine_id, engine_name=engine_name, module=module)


def registry(reload: bool = False) -> dict[str, ParserInfo]:
    """返回 ``{module_name: ParserInfo}``，按需导入并缓存。

    模块清单来自 :func:`discover_modules` —— 自动发现，不写死。
    """
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = {}
        for name in discover_modules():
            info = _load_one(name)
            _REGISTRY[name] = info
    return _REGISTRY


def get(engine_id: str) -> ModuleType | None:
    info = registry().get(engine_id)
    if info and info.ok:
        return info.module
    # 也允许用模块里声明的 ENGINE_ID 来找
    for info in registry().values():
        if info.ok and info.engine_id == engine_id:
            return info.module
    return None


def engine_name(engine_id: str) -> str:
    info = registry().get(engine_id)
    if info:
        return info.engine_name
    for info in registry().values():
        if info.engine_id == engine_id:
            return info.engine_name
    return engine_id


def list_engines() -> list[dict[str, Any]]:
    out = []
    for name, info in registry().items():
        out.append(
            {
                "key": name,
                "id": info.engine_id,
                "name": info.engine_name,
                "available": info.ok,
                "error": info.error,
            }
        )
    return out


# --------------------------------------------------------------------------
# 调用封装：屏蔽各引擎模块的签名差异
# --------------------------------------------------------------------------
def _signature_flags(module: ModuleType) -> tuple[bool, bool]:
    try:
        params = inspect.signature(module.extract_lines).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False, False
    return "encoding" in params, "options" in params


def detect(root: pathlib.Path) -> list[tuple[str, int]]:
    """对所有可用解析器跑 ``detect_dir``，按置信度降序返回。"""
    scores: list[tuple[str, int]] = []
    for key, info in registry().items():
        if not info.ok:
            continue
        fn = getattr(info.module, "detect_dir", None)
        if fn is None:
            continue
        try:
            score = int(fn(root))
        except Exception as exc:
            log.debug("detect_dir(%s) 失败：%s", key, exc)
            score = 0
        if score > 0:
            scores.append((key, max(0, min(100, score))))
    scores.sort(key=lambda kv: (-kv[1], kv[0]))
    return scores


def iter_scripts(engine_id: str, root: pathlib.Path, **kwargs: Any) -> Iterator[tuple[str, bytes, str]]:
    """调用某个解析器的 ``iter_scripts``，统一产出 ``(virtual_path, data, container)``。"""
    module = get(engine_id)
    if module is None:
        return
    fn = getattr(module, "iter_scripts", None)
    if fn is None:
        return
    try:
        params = inspect.signature(fn).parameters
        accepts_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        call_kwargs = kwargs if accepts_kwargs else {k: v for k, v in kwargs.items() if k in params}
    except (TypeError, ValueError):  # pragma: no cover
        call_kwargs = {}

    try:
        iterator = fn(root, **call_kwargs)
    except Exception as exc:
        log.warning("iter_scripts(%s) 初始化失败：%s", engine_id, exc)
        return

    origin = str(root)
    while True:
        try:
            item = next(iterator)  # type: ignore[arg-type]
        except StopIteration:
            return
        except Exception as exc:
            log.warning("iter_scripts(%s) 中断：%s", engine_id, exc)
            return
        if item is None:
            continue
        if isinstance(item, (tuple, list)):
            if len(item) >= 3:
                virtual_path, data, container = item[0], item[1], item[2]
            elif len(item) == 2:
                virtual_path, data = item
                container = ""
            else:
                continue
        else:
            continue
        if not isinstance(data, (bytes, bytearray)):
            continue
        yield str(virtual_path), bytes(data), str(container or origin)


def pick_extractor(engine_id: str, virtual_path: str) -> ModuleType | None:
    """决定用哪个模块来抽文本。

    散装文件（plaintext 引擎产出的）按扩展名路由给专业解析器，
    例如目录里的 ``scenario/foo.ks`` 虽然由 plaintext 发现，
    但仍然交给 kirikiri 模块解析 KAG 语法。
    """
    if engine_id != "plaintext":
        return get(engine_id)
    ext = pathlib.PurePosixPath(virtual_path).suffix.lower()
    routed = EXTENSION_ROUTES.get(ext)
    if routed:
        module = get(routed)
        if module is not None:
            return module
    return get("plaintext")


def call_extract_lines(
    module: ModuleType,
    virtual_path: str,
    data: bytes,
    options: textkit.CleanOptions,
    encoding: str = "auto",
) -> list[tuple[str, str]]:
    """调用模块的 ``extract_lines`` 并统一成 ``(说话人, 台词)``。

    各引擎模块只负责「把脚本里的字符串捞出来」，这里的收尾工作
    （拆说话人、标签清理、长度/语言过滤）由 textkit 统一做，
    保证不同引擎的输出风格一致。
    """
    fn = getattr(module, "extract_lines", None)
    if fn is None:
        return []
    accepts_encoding, accepts_options = _signature_flags(module)
    kwargs: dict[str, Any] = {}
    if accepts_encoding and encoding and encoding.lower() != "auto":
        kwargs["encoding"] = encoding
    if accepts_options:
        kwargs["options"] = options
    try:
        raw = fn(virtual_path, data, **kwargs)
    except TypeError:
        try:
            raw = fn(virtual_path, data)
        except Exception as exc:
            log.warning("extract_lines(%s) 失败：%s", virtual_path, exc)
            return []
    except Exception as exc:
        log.warning("extract_lines(%s) 失败：%s", virtual_path, exc)
        return []
    if not raw:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        speaker = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            speaker, text = str(item[0] or ""), str(item[1] or "")
        elif isinstance(item, dict):
            speaker = str(item.get("speaker") or "")
            text = str(item.get("text") or "")
        else:
            continue
        if not text:
            continue
        for line in text.splitlines():
            for sp, body in textkit.extract_dialogue(line, options):
                if body in seen:
                    continue
                seen.add(body)
                out.append((textkit.normalize_text(speaker) or sp, body))
    return out

"""测试用的合成游戏目录。

不依赖任何真实游戏：各引擎的样本都按官方格式规范逐字节现造，
这样测试既能跑在 CI 上，也能真正验证解析器而不是验证「文件存在」。
"""

from __future__ import annotations

import pathlib
import random

from galtext import parsers

KS_PROLOGUE = (
    "*start\n"
    "悠斗「おはよう、先輩。今日もいい天気だね」\n"
    "空は青く澄み渡っていた。\n"
    "[wait time=500]\n"
    "先輩「おはよう。今日は早いのね」\n"
)

KS_CHAPTER1 = (
    "「ここはどこだろう…」\n"
    "; コメント行\n"
    '[if exp="f.flag"]\n'
    "謎の少女「ようこそ、喫茶店へ」\n"
)

KS_LOOSE = "散装脚本「これは封包の外にある台詞です」\n"

NSCRIPT = (
    "*define\n"
    "game\n"
    "*start\n"
    'bg "bg01.jpg",1\n'
    "「おはよう、先輩。今日もいい天気だね」\n"
    "click\n"
    "mesbox \"選択してください\",1\n"
    "wait 300\n"
)


def make_kirikiri_archive() -> bytes | None:
    """造一个 XP3 封包（内含明文 + zlib 压缩两个 .ks）。"""
    kk = parsers.get("kirikiri")
    if kk is None or not hasattr(kk, "_build_xp3"):
        return None
    members = [
        {
            "name": "scenario/prologue.ks",
            "data": KS_PROLOGUE.encode("cp932"),
            "compress": False,
            "encrypt": False,
            "null_terminated": True,
        },
        {
            "name": "scenario/chapter1.ks",
            "data": KS_CHAPTER1.encode("cp932"),
            "compress": True,
            "encrypt": False,
            "null_terminated": False,
        },
    ]
    return kk._build_xp3(members, compress_index=False)


def make_nscript_dat() -> bytes | None:
    ns = parsers.get("nscripter")
    if ns is None or not hasattr(ns, "_build_nscript_dat"):
        return None
    return ns._build_nscript_dat()


def make_mixed_game(base: pathlib.Path, with_noise: bool = True) -> pathlib.Path:
    """造一个「KiriKiri + NScripter + 散装脚本」的混合目录。"""
    game = pathlib.Path(base) / "SampleGame"
    (game / "scenario").mkdir(parents=True, exist_ok=True)

    archive = make_kirikiri_archive()
    if archive is not None:
        (game / "data.xp3").write_bytes(archive)

    script = make_nscript_dat()
    if script is not None:
        (game / "nscript.dat").write_bytes(script)

    (game / "scenario" / "loose.ks").write_bytes(KS_LOOSE.encode("cp932"))

    if with_noise:
        rng = random.Random(20240924)
        (game / "noise.bin").write_bytes(bytes(rng.randrange(256) for _ in range(8192)))

    return game


def expected_japanese_phrases() -> list[str]:
    """上面样本里一定应该被抽出来的台词。"""
    return [
        "おはよう、先輩。今日もいい天気だね",
        "空は青く澄み渡っていた。",
        "おはよう。今日は早いのね",
        "ここはどこだろう…",
        "ようこそ、喫茶店へ",
        "これは封包の外にある台詞です",
        "選択してください",
    ]


# --------------------------------------------------------------------------
# 「原版 + 汉化补丁并存」的样本
# --------------------------------------------------------------------------
# 这是汉化 galgame 最常见的形态：data.xp3 是日文原版，patch.xp3 是汉化补丁，
# 里面装着**同名**脚本。引擎按文件名顺序加载，patch 盖掉 data。
PATCH_BASE_KS = (
    "*start\n"
    "悠斗「おはよう、先輩。今日もいい天気だね」\n"
    "空は青く澄み渡っていた。\n"
    "先輩「おはよう。今日は早いのね」\n"
)

PATCH_CN_KS = (
    "*start\n"
    "悠斗「早上好，前辈。今天天气也不错呢」\n"
    "天空湛蓝清澈。\n"
    "前辈「早上好。今天来得真早」\n"
)

PATCH_BASE_LOOSE = "悠斗「これは封包の外にある台詞です」\n"
PATCH_CN_LOOSE = "悠斗「这是封包外的台词」\n"

#: 抽取后的纯台词（说话人会被拆出去，所以断言要用这个而不是整行）
PATCH_JP_LOOSE_TEXT = "これは封包の外にある台詞です"
PATCH_CN_LOOSE_TEXT = "这是封包外的台词"

PATCH_CN_PHRASES = [
    "早上好，前辈。今天天气也不错呢",
    "天空湛蓝清澈。",
    "早上好。今天来得真早",
]
PATCH_JP_PHRASES = [
    "おはよう、先輩。今日もいい天気だね",
    "空は青く澄み渡っていた。",
    "おはよう。今日は早いのね",
]


def make_patched_game(base: pathlib.Path, loose_override: bool = True) -> pathlib.Path | None:
    """造一个「原版 + 汉化补丁并存」的 KiriKiri 目录。

    * ``data.xp3``  —— 日文原版，含 ``scenario/prologue.ks`` 与 ``scenario/loose.ks``
    * ``patch.xp3`` —— 汉化补丁，装**同名**脚本（中文，GBK 编码）
    * ``scenario/loose.ks`` —— 散装的中文版，用来验证「散装 > 封包」的优先级

    kirikiri 模块不可用时返回 ``None``，由调用方决定是否 skip。
    """
    kirikiri = parsers.get("kirikiri")
    if kirikiri is None or not hasattr(kirikiri, "_build_xp3"):
        return None

    game = pathlib.Path(base) / "PatchedGame"
    (game / "scenario").mkdir(parents=True, exist_ok=True)

    base_members = [
        {
            "name": "scenario/prologue.ks",
            "data": PATCH_BASE_KS.encode("cp932"),
            "compress": False,
            "encrypt": False,
            "null_terminated": True,
        },
        {
            "name": "scenario/loose.ks",
            "data": PATCH_BASE_LOOSE.encode("cp932"),
            "compress": True,
            "encrypt": False,
            "null_terminated": False,
        },
    ]
    patch_members = [
        {
            "name": "scenario/prologue.ks",
            "data": PATCH_CN_KS.encode("cp936"),
            "compress": False,
            "encrypt": False,
            "null_terminated": True,
        },
        {
            "name": "scenario/loose.ks",
            "data": PATCH_CN_LOOSE.encode("cp936"),
            "compress": True,
            "encrypt": False,
            "null_terminated": False,
        },
    ]
    (game / "data.xp3").write_bytes(kirikiri._build_xp3(base_members, compress_index=False))
    (game / "patch.xp3").write_bytes(kirikiri._build_xp3(patch_members, compress_index=True))

    # 散装文件优先级最高：引擎会优先读封包外的同名文件
    if loose_override:
        (game / "scenario" / "loose.ks").write_bytes(PATCH_CN_LOOSE.encode("cp936"))

    return game

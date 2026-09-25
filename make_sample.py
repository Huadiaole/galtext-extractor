"""生成一个示例「游戏目录」，用来立刻试用 GalText Extractor。

::

    python make_sample.py            # 生成到 ./sample_game
    python make_sample.py D:\\tmp\\x  # 生成到指定目录
    python -m galtext gui sample_game

生成的内容全部是**合成的**，不含任何真实游戏数据：

* ``data.xp3``            —— 按 XP3 规范逐字节构造，内含 6 个 .ks（明文与 zlib 压缩混排）
* ``nscript.dat``         —— 按 NScripter 规范用 XOR 0x84 加密
* ``scenario/loose.ks``   —— 散装脚本（走 plaintext 解析器）
* ``noise.bin``           —— 随机二进制，用来验证噪声不会被误判成文本

跑完之后就能在界面上看到引擎识别、脚本列表、说话人拆分与导出的完整效果。
"""

from __future__ import annotations

import pathlib
import random
import sys

# 允许直接从项目根目录运行
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from galtext import parsers  # noqa: E402

KS_PROLOGUE = """*start
悠斗「おはよう、先輩。今日もいい天気だね」
空は青く澄み渡っていた。
[wait time=500]
先輩「おはよう。今日は早いのね」
悠斗「早起きは三文の得って言うだろ」
先輩「……その言葉、使い方が違うわよ」
[r]
放課後の教室は、いつもより少しだけ静かだった。
"""

KS_CHAPTER1 = """「ここはどこだろう…」
; コメント行は抽出されない
[if exp="f.flag"]
謎の少女「ようこそ、喫茶店へ」
悠斗「君は、こんなところで何を？」
少女「待っているの。ずっと、ずっと前から」
悠斗「誰を？」
少女「それは、あなたが決めることよ」
"""

KS_CHAPTER2 = """*scene_kyoushitsu
窓の外では、雨が静かに降りつづけていた。
先輩「傘、持ってないの？」
悠斗「朝は晴れてたんですよ」
先輩「じゃあ、一緒に帰りましょうか」
悠斗「……いいんですか」
先輩「風邪をひかれたら困るもの」
[r]
傘はひとつしかなかった。
"""

KS_CHAPTER3 = """*scene_yuugure
屋上にのぼると、夕日がちょうど沈むところだった。
少女「遅い。待ちくたびれたわ」
悠斗「ごめん、委員会が長引いて」
少女「言い訳はいいから。ほら、見て」
悠斗「……きれいだ」
少女「でしょう。これを見せたかったの」
少女「わたしが、ここにいる理由のひとつ」
"""

KS_EPILOGUE = """*scene_ending
そして、夏が終わろうとしていた。
悠斗「また、会えるよね」
少女「さあ。どうかしら」
少女「でも、忘れられないのは嫌だから」
少女「だから、これはあげる」
悠斗「これは……鈴？」
少女「そう。わたしの名前」
[r]
その音は、いつまでも耳の奥で鳴りつづけていた。
"""

KS_LOOSE = "散装脚本「これは封包の外にある台詞です」\n"

NSCRIPT = """*define
game
*start
bg "bg01.jpg",1
「おはよう、先輩。今日もいい天気だね」
click
mesbox "選択してください",1
wait 300
*scene2
「この封包は、NScripter の形式で作られています」
click
end
"""


def build(target: pathlib.Path) -> pathlib.Path:
    game = target / "SampleGame"
    (game / "scenario").mkdir(parents=True, exist_ok=True)

    kirikiri = parsers.get("kirikiri")
    if kirikiri is not None and hasattr(kirikiri, "_build_xp3"):
        # 明文 / zlib 压缩混排，顺便把两条解包路径都覆盖到
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
            {
                "name": "scenario/chapter2.ks",
                "data": KS_CHAPTER2.encode("cp932"),
                "compress": True,
                "encrypt": False,
                "null_terminated": True,
            },
            {
                "name": "scenario/chapter3.ks",
                "data": KS_CHAPTER3.encode("cp932"),
                "compress": False,
                "encrypt": False,
                "null_terminated": True,
            },
            {
                "name": "scenario/epilogue.ks",
                "data": KS_EPILOGUE.encode("cp932"),
                "compress": True,
                "encrypt": False,
                "null_terminated": True,
            },
        ]
        (game / "data.xp3").write_bytes(kirikiri._build_xp3(members, compress_index=False))
        print(f"  data.xp3          {(game / 'data.xp3').stat().st_size:>7} 字节（5 个脚本）")
    else:
        print("  [跳过] kirikiri 模块不可用，无法生成 XP3")

    nscripter = parsers.get("nscripter")
    if nscripter is not None and hasattr(nscripter, "_build_nscript_dat"):
        (game / "nscript.dat").write_bytes(nscripter._build_nscript_dat())
        print(f"  nscript.dat       {(game / 'nscript.dat').stat().st_size:>7} 字节")
    else:
        print("  [跳过] nscripter 模块不可用")

    (game / "scenario" / "loose.ks").write_bytes(KS_LOOSE.encode("cp932"))
    print(f"  scenario/loose.ks {(game / 'scenario' / 'loose.ks').stat().st_size:>7} 字节")

    rng = random.Random(20240924)
    (game / "noise.bin").write_bytes(bytes(rng.randrange(256) for _ in range(8192)))
    print(f"  noise.bin         {(game / 'noise.bin').stat().st_size:>7} 字节（随机噪声）")

    return game


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path.cwd() / "sample_game"
    print(f"正在生成示例游戏目录：{target}")
    game = build(target)
    print()
    print("完成！现在可以这样试用：")
    print(f'  python -m galtext gui "{game}"')
    print(f'  python -m galtext scan "{game}" -o sample.csv -f template')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

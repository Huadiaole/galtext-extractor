"""PyInstaller 打包入口。

以 ``python -m galtext`` 直接打包时 PyInstaller 对包的识别不够稳，
所以单独放一个脚本当入口。

打包分两种模式，行为不一样：

* ``--console`` 构建（``galtext.exe``）—— 完整命令行，正常打印到控制台；
* ``--windowed`` 构建（``GalTextExtractor.exe``）—— **没有控制台**，
  ``sys.stdout`` 是 ``None``。这时候如果还按命令行走，argparse 会往 ``None``
  里写、抛出异常，而 windowed 模式的 PyInstaller 会把未捕获异常弹成一个
  阻塞的错误对话框 —— 双击后看起来就是「卡死」。所以这里必须显式分流。
"""

from __future__ import annotations

import multiprocessing
import sys


def _warn_no_console() -> None:
    """windowed 构建下被当成命令行用时，给一句人话提示而不是崩掉。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "GalText Extractor",
            "这个 exe 是图形界面版本，不带控制台输出，无法显示命令行结果。\n\n"
            "· 想用命令行：请用同目录下的 galtext.exe，"
            "或直接跑 python -m galtext\n"
            "· 想用界面：直接双击本程序即可",
        )
        root.destroy()
    except Exception:  # pragma: no cover - 连 Tk 都起不来就算了
        pass


def main() -> int:
    multiprocessing.freeze_support()
    import pathlib

    from galtext.cli import main as cli_main

    # windowed 构建里 stdout 是 None
    headless = sys.stdout is None
    args = sys.argv[1:]

    if not headless:
        # 控制台构建：不带参数时也默认进图形界面
        return cli_main(args or ["gui"])

    if not args:
        return cli_main(["gui"])
    if args[0] == "gui":
        return cli_main(args)
    # 把文件夹拖到 exe 上（或命令行传目录）是很自然的用法，别当成误用 CLI：
    # 直接开界面并扫描这个目录。
    if len(args) == 1 and pathlib.Path(args[0]).is_dir():
        return cli_main(["gui", args[0]])

    _warn_no_console()
    return cli_main(["gui"])


if __name__ == "__main__":
    raise SystemExit(main())

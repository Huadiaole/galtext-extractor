<img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/banner.png" width="100%" alt="GalText Extractor">

# GalText Extractor 1.1.0

**从 galgame 安装目录提取对话文本，并排版成可阅读、可搜索、可打印的剧本 PDF。**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)

---

## 它做什么

galgame 的文本通常锁在 XP3 / NSA / arc 这类封包里，还常常是 Shift-JIS 再叠一层异或混淆；
想通读一遍剧情、做翻译或做文本统计，都得先把这层壳去掉。

这个工具负责三件事：

1. **解析** —— 识别引擎，解开封包，还原脚本，自动判定编码；
2. **整理** —— 剥离标签、拆分说话人、过滤代码行、全局去重；
3. **输出** —— 导出 TXT / CSV / JSON / 翻译模板，或者直接排版成剧本 PDF。

纯 Python 标准库实现，**零运行时依赖**。不注入进程、不修改游戏文件、不联网。

---

## 这一版的重点

### 原版与汉化补丁并存时，不再混入原文

汉化补丁通常叠加在原版之上：`data.xp3` 是日文原版，`patch.xp3` 是汉化补丁，
两者装着**同名脚本**。引擎按文件名顺序加载、后者覆盖前者，本工具遵循同样的语义：

- 同名脚本只保留优先级最高的那份，输出直接是中文；
- 优先级：散装文件 > 封包；封包之间按文件名顺序，靠后的覆盖靠前的；
- 被覆盖的脚本不会静默丢弃，会以「被 ××× 覆盖，已跳过」列入报告；
- 需要对照原文与译文时，可以关闭「封包覆盖」（`--keep-all-versions`）。

如果补丁形式特殊、覆盖语义不适用，还有「只保留中文行」作为兜底
（`--chinese-only`，按假名过滤）。扫描结束时会告诉你有多少条文本含假名、占比多少。

### 三种说话人写法都支持

| 写法 | 示例 | 默认 |
| --- | --- | --- |
| 名字与台词同一行 | `悠斗「早上好」` | 支持 |
| 名字用标签单独声明 | `[name text="悠斗"]` ⏎ `早上好` | 支持，会沿用到下一个人名 |
| 名字单独成行 | `悠斗` ⏎ `早上好` | 需开启「自动识别单独成行的角色名」 |

第二种最容易出问题：声明行本质是一个标签，很容易被当作垃圾整行丢弃，
导致后续大段台词都失去人名。这一版专门处理了它。

旁白本身没有说话人。若希望每行都带名字，在「旁白标记」中填入 `旁白`
（`--narration-label 旁白`），旁白也会署名，各导出格式都会带上。

### 剧本风格 PDF

![剧本 PDF 效果](https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/script-pdf.png)

场景标题、角色名、台词、旁白各有样式，带页码，并遵守中文禁则折行
（`。，、` 不会出现在行首，`（「` 不会留在行尾），英文单词不会被从中间截断。

PDF 写入器是自研的，不依赖 ReportLab。中文字体按实际用字做**字形子集化**后嵌入：

| 字体 | 原始大小 | 50 条台词 | 400 条台词 |
| --- | --- | --- | --- |
| SimHei（黑体） | 9.29 MB | 35 KB | ~60 KB |
| Microsoft YaHei UI（微软雅黑） | 18.79 MB | 217 KB | ~300 KB |

同时写入了 `/ToUnicode` 映射 —— **PDF 里的中文可以复制，也可以用 Ctrl+F 搜索**。

### 界面

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/screenshot.png" alt="浅色模式"><br><div align="center">浅色</div></td>
<td width="50%"><img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/screenshot-dark.png" alt="深色模式"><br><div align="center">深色</div></td>
</tr>
</table>

明暗双主题、自绘图标、后台线程扫描、实时搜索，角色名与旁白分色显示。
安装了 `sv-ttk` 会自动切换为 Fluent 风格；未安装也不影响使用，内置主题同样完整。

---

## 支持的引擎

| 引擎 | 封包 | 说明 |
| --- | --- | --- |
| **KiriKiri / KAG** | XP3 | 含官方 hash-XOR 过滤器 |
| **NScripter / ONScripter** | NSA / SAR | XOR `0x84`，成员 LZSS 解压 |
| **BGI / Ethornell** | arc v1 / v2 | 含 DSC 的 key 流与 Huffman 解压 |
| 散装脚本 | — | `.ks` / `.txt` / `.ws2` 等 |
| 通用二进制扫描 | — | 兜底：直接在二进制中扫描文本串 |

编码自动识别 Shift-JIS / GBK / Big5 / UTF-8 / UTF-16（含无 BOM）。

RealLive 尚未实现。它在注册表中保留位置，`galtext engines` 会如实标注为「不可用」，
补齐同名模块即可自动生效。

---

## 使用方式

```bash
# 图形界面
GalTextExtractor.exe

# 命令行导出 CSV
galtext.exe scan "D:\Games\SomeGame" -o out.csv -f csv

# 直接生成剧本 PDF
galtext.exe scan "D:\Games\SomeGame" -f pdf -o script.pdf
```

手头没有合适的游戏时，可以先造一个合成样本（不含任何真实游戏数据）：

```bash
python make_sample.py
python -m galtext gui sample_game\SampleGame
```

源码方式运行（需要 Python 3.10+，无需安装任何依赖）：

```bash
git clone https://github.com/Huadiaole/galtext-extractor.git
cd galtext-extractor
python -m galtext gui
```

---

## 下载

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `GalTextExtractor.exe`（图形界面） | 11.89 MB | `186182a1e0fc6730f38c606d97f688bcc37a99b2130ae8345483395499114ae4` |
| `galtext.exe`（命令行） | 11.93 MB | `c8d641db3fd2f9e21e2a2b71ad7363ad31ea0a95190d7f5dbddf1196faa0c83a` |

两个 exe 均为 PyInstaller 单文件构建，首次运行会解包到 `%TEMP%`，因此启动稍慢，
且需要 `%TEMP%` 可写。

---

## 边界与限制

- 只处理**静态脚本**：文本全在加密资源中、或运行时动态拼装的游戏无效；
- 不做 OCR，也不钩取运行中的游戏进程；
- 不附带、不索引、不分发任何游戏本体、汉化补丁或文本资源；
- 不联网，不上传任何数据；
- 韩文不做自动识别（CP949 与 GBK 的字节空间几乎完全重叠），
  需要时用 `--force-encoding cp949` 显式指定；
- 通用二进制扫描按字节流切串，串首串尾偶尔会粘上一个乱码字；
- 剧本 PDF 只支持 TrueType 字体（`.otf` 的 CFF 轮廓无法子集化）。

---

## 关于

**作者：全部化掉了** · MIT License

格式细节参考了这些项目：
[GARbro](https://github.com/morkt/GARbro)、
[arc_unpacker](https://github.com/vn-tools/arc_unpacker)、
[KiriKiri2](https://github.com/krkrz/krkr2)、
[ONScripter](https://github.com/ogapee/onscripter)。

136 个测试全部基于**合成样本**（按各引擎的格式规范逐字节构造），不依赖任何真实游戏，
因此 CI 通过意味着解析器本身被验证过，而不只是文件存在。

使用中遇到问题，或你的游戏无法识别，欢迎开 issue，说明引擎与目录结构即可。

> 仅供个人学习、研究与翻译使用。请在自己合法拥有的游戏副本上使用，
> 不要传播提取出的原始资源。**游戏文本版权归原厂商所有。**

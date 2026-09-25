# GalText Extractor 1.1.0

**从 galgame 安装目录提取对话文本，并排版成可阅读、可打印、可搜索的剧本 PDF。**

纯 Python 标准库实现，零运行时依赖、离线可用、不注入进程、不修改游戏文件、不联网。

---

## 这一版的重点

### 🎯 汉化游戏：不再混进日文原文

很多汉化补丁是**叠加**在原版上的（`data.xp3` 日文原版 + `patch.xp3` 汉化补丁，
装着同名脚本）。引擎按文件名顺序加载、后者覆盖前者 —— 本工具现在遵循同样的语义：

- 同名脚本**只保留优先级最高的那份**，输出直接是中文；
- 优先级：散装文件 > 封包；封包之间按文件名顺序，靠后的覆盖靠前的；
- 被覆盖的脚本**不会悄悄消失**，会以「被 ××× 覆盖，已跳过」列在报告里；
- 想对照原文与译文，关掉「封包覆盖」即可（CLI：`--keep-all-versions`）。

补丁形式特殊、覆盖语义失效时，还有兜底开关**「只保留中文行」**
（CLI：`--chinese-only`）—— 按「日文行必定含假名」这一点滤掉原文。
注意这是启发式，中文里残留的假名会一起被丢掉。

### 📕 剧本风格 PDF

零依赖自研 PDF 写入器（不需要 ReportLab），自动挑系统中文字体并做**字形子集化**：

| 字体 | 原始大小 | 50 条台词 | 400 条台词 |
| --- | --- | --- | --- |
| SimHei（黑体） | 9.29 MB | 35 KB | ~60 KB |
| Microsoft YaHei UI（微软雅黑） | 18.79 MB | 217 KB | ~300 KB |

排版为场景标题 / 角色名 / 台词 / 旁白 + 页码，遵守中文禁则折行，
并带 `/ToUnicode` 映射 —— **PDF 里的中文可以复制、可以 Ctrl+F 搜索**。

### 🎨 界面

明暗双主题（内置自绘主题，装了 `sv-ttk` 自动升级 Fluent 风格）、
应用图标与 12 个工具栏图标、后台线程扫描、实时搜索、说话人/旁白分色预览。

---

## 下载

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `GalTextExtractor.exe`（图形界面，双击即用） | 11.88 MB | `41bdd2bd1eb3e3844a8556804cd0949e7d3ead27b7026eb86e6efc948a0ce9d5` |
| `galtext.exe`（命令行） | 11.92 MB | `6e40df8524e33b6cb958bec7f664da5cf73ffdf957617d9a5f8634e75b971caa` |

> 两个 exe 都是 PyInstaller 单文件构建，首次运行会把自身解包到 `%TEMP%`，
> 因此启动稍慢，且**需要 `%TEMP%` 可写**。
> 如果所在环境限制了临时目录，可以改用源码方式运行（见下）。

校验：

```bash
sha256sum GalTextExtractor.exe galtext.exe     # Linux/macOS
certutil -hashfile GalTextExtractor.exe SHA256 # Windows
```

---

## 支持的引擎

| 引擎 | 封包 | 脚本 | 加密 / 压缩 |
| --- | --- | --- | --- |
| KiriKiri / KAG | XP3 | `.ks` / `.tjs` | 官方 XP3 hash-XOR 过滤器 |
| NScripter / ONScripter | NSA / SAR | `nscript.dat`(XOR `0x84`)、`00.txt` | LZSS（Okumura 变体） |
| BGI / Ethornell | arc v1 / v2 | `.ws2` / `.dsc` | DSC 的 key 流 + Huffman |
| 散装脚本 | — | `.ks` / `.txt` / `.ws2` 等 | — |
| 通用二进制扫描 | — | 任意二进制中的文本串 | — |

编码自动探测：Shift-JIS / GBK / Big5 / UTF-8 / UTF-16（含无 BOM）。

> RealLive 仍未实现。`galtext engines` 会把它排在最后并标注「不可用」——
> 这是有意预留的扩展位，补上同名模块即自动生效。

---

## 快速开始

```bash
# 图形界面
GalTextExtractor.exe

# 命令行
galtext.exe scan "D:\Games\SomeGame" -o out.csv -f csv
galtext.exe scan "D:\Games\SomeGame" -f pdf -o script.pdf
galtext.exe scan "D:\Games\SomeGame" --chinese-only -o cn.txt
```

源码方式（需要 Python 3.10+，无需安装任何依赖）：

```bash
git clone https://github.com/Huadiaole/galtext-extractor.git
cd galtext-extractor
python -m galtext gui
```

---

## 已知限制

- 只处理**静态脚本**：文本全在加密资源里、或运行时动态拼装文本的游戏无效。
- 通用二进制扫描按字节流切串，串首/串尾偶尔会粘上一个乱码字。
- 韩文不做自动识别（CP949 与 GBK 字节空间几乎完全重叠），需要时用
  `--force-encoding cp949` 指定。
- 剧本 PDF 只支持 TrueType 字体（`.otf` 的 CFF 轮廓无法子集化，会明确报错）。
- 剧本 PDF 是纯中文单栏，不做中日对照与表格排版。
- Windows 原生菜单栏不跟随深色主题（Tk 平台限制）。

---

## 许可与作者

**作者：全部化掉了** · [MIT License](https://github.com/Huadiaole/galtext-extractor/blob/main/LICENSE)

格式细节参考了 [GARbro](https://github.com/morkt/GARbro)、
[arc_unpacker](https://github.com/vn-tools/arc_unpacker)、
[KiriKiri2](https://github.com/krkrz/krkr2)、
[ONScripter](https://github.com/ogapee/onscripter) 的工作。

本工具仅供个人学习、研究与翻译使用。请在自己合法拥有的游戏副本上使用，
不要传播提取出的原始资源。**游戏文本版权归原厂商所有。**

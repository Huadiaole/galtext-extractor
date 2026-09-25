<img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/banner.png" width="100%" alt="GalText Extractor">

# 🎉 GalText Extractor 1.1.0 发布

**把 galgame 的剧本从封包里抠出来，再排成一本能读、能搜、能打印的 PDF。**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)

---

## 这玩意儿到底干嘛的

你有没有过这种时刻：

- 想重温一段剧情，但实在不想再点一遍游戏；
- 想翻译，结果文本锁在 `data.xp3` 里，还是 Shift-JIS 套一层 XOR；
- 想统计某角色到底说了多少句话，打开脚本一看全是 `[wait time=500]`。

这个工具就干三件事：

1. **拆** —— 认出引擎，解开封包，把脚本还原出来（编码自动判定）
2. **捡** —— 剥标签、拆说话人、滤掉代码行、全局去重
3. **装** —— 导出 TXT / CSV / JSON / 翻译模板，或者直接排成**剧本 PDF**

> 纯 Python 标准库，**零运行时依赖**。不注入进程、不改游戏文件、不联网。

---

## 四个我觉得最值钱的点

### 1️⃣ 「原版 + 汉化补丁」不再混成一锅

汉化补丁通常是**叠**在原版上的：`data.xp3` 是日文，`patch.xp3` 是中文，里面装着同名脚本。

如果你直接把两个封包都扒一遍，就会得到原文和译文混在一起的玩意儿。但引擎其实只加载优先级高的那份——
所以这个工具也照着来：**同名脚本只留赢家**，输出直接是中文。

被盖掉的那些**不会悄悄消失**，会在报告里写明「被 ××× 覆盖，已跳过」。
想拿原文和译文对照？关掉「封包覆盖」就行。

补丁形式太野、覆盖语义没起作用的时候，还有「只保留中文行」兜底（按假名过滤）。
剩下的日文到底有多少，扫描完会直接告诉你占比。

### 2️⃣ 说话人认得出来，每句都知道是谁说的

galgame 脚本认人名有三种写法，我们都支持：

| 写法 | 长这样 |
| --- | --- |
| 名字在同一行 | `悠斗「早上好」` |
| 名字单独声明 | `[name text="悠斗"]` ⏎ `早上好` |
| 名字单独成行 | `悠斗` ⏎ `早上好` |

第二种最坑——**声明行是个标签，很容易被当垃圾丢掉**，然后后面一大片台词全都没名字。
1.1.0 专门修了这个：声明会一直沿用到下一个人名出现。

旁白本来就没有说话人。想「每句都带名字」？在「旁白标记」里填 `旁白` 就行。

### 3️⃣ 剧本 PDF：把台词排成能读的东西

不是把 txt 硬塞进 PDF，而是真的按剧本来排：

![剧本 PDF 效果](https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/script-pdf.png)

场景标题 / 角色名 / 台词 / 旁白各有样式，带页码，遵守中文禁则折行
（`。，、` 不会跑到行首，`（「` 不会留在行尾），英文单词不会被从中间截断，角色名不会被单独留在页尾。

而且它是**零依赖自研的 PDF 写入器**——不用 ReportLab，中文字体照嵌不误，还做了**字形子集化**：

| 字体 | 原始 | 50 条台词 | 400 条台词 |
| --- | --- | --- | --- |
| SimHei 黑体 | 9.29 MB | **35 KB** | ~60 KB |
| 微软雅黑 | 18.79 MB | **217 KB** | ~300 KB |

顺手还带了 `/ToUnicode` 映射，所以 **PDF 里的中文能复制、能 Ctrl+F 搜**。

### 4️⃣ 顺便，界面是好看的（这次的意外收获）

<table>
<tr>
<td width="50%"><img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/screenshot.png" alt="浅色模式"><br><div align="center">浅色</div></td>
<td width="50%"><img src="https://raw.githubusercontent.com/Huadiaole/galtext-extractor/main/docs/screenshot-dark.png" alt="深色模式"><br><div align="center">深色</div></td>
</tr>
</table>

明暗双主题、自绘图标、后台线程扫描、实时搜索、角色名和旁白分色。
装了 `sv-ttk` 会自动升级成 Fluent 风格——**没装也完全不影响**，内置主题照样能看。

---

## 认识哪几种游戏

| 引擎 | 封包 | 备注 |
| --- | --- | --- |
| **KiriKiri / KAG** | XP3 | 含官方 hash-XOR 过滤器 |
| **NScripter / ONScripter** | NSA / SAR | XOR `0x84` + 成员 LZSS 解压 |
| **BGI / Ethornell** | arc v1 / v2 | 含 DSC 的 key 流 + Huffman 解压 |
| 散装脚本 | — | `.ks` / `.txt` / `.ws2` 等 |
| 通用二进制扫描 | — | 兜底：直接在二进制里扫文本串 |

编码自动识别 Shift-JIS / GBK / Big5 / UTF-8 / UTF-16（含无 BOM）。
RealLive 还没写，但它占着位置，`galtext engines` 会如实告诉你是「不可用」。

---

## 三行命令就能用

```bash
# 图形界面
GalTextExtractor.exe

# 命令行导出 CSV
galtext.exe scan "D:\Games\SomeGame" -o out.csv -f csv

# 直接出剧本 PDF
galtext.exe scan "D:\Games\SomeGame" -f pdf -o script.pdf
```

没有游戏可试？先造一个合成样本（不含任何真实游戏数据）：

```bash
python make_sample.py
python -m galtext gui sample_game\SampleGame
```

---

## 下载

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `GalTextExtractor.exe`（图形界面，双击即用） | 11.89 MB | `186182a1e0fc6730f38c606d97f688bcc37a99b2130ae8345483395499114ae4` |
| `galtext.exe`（命令行） | 11.93 MB | `c8d641db3fd2f9e21e2a2b71ad7363ad31ea0a95190d7f5dbddf1196faa0c83a` |

或者直接跑源码（Python 3.10+，**不需要 pip install 任何东西**）：

```bash
git clone https://github.com/Huadiaole/galtext-extractor.git
cd galtext-extractor
python -m galtext gui
```

---

## 它不做什么

写清楚免得误会：

- ❌ **不 OCR、不钩取运行中的游戏** —— 只做静默的脚本/封包解析
- ❌ **不提供任何游戏资源或汉化补丁**
- ❌ **不联网**，不上传你的任何东西
- ❌ 对「文本全在加密资源里」或「运行时动态拼装文本」的游戏无效
- ⚠️ 韩文不做自动识别（CP949 和 GBK 字节空间几乎完全重合，硬猜会把简中判成韩文）

---

## 最后

**作者：全部化掉了** · MIT License

格式细节站在这些项目的肩膀上：
[GARbro](https://github.com/morkt/GARbro)、[arc_unpacker](https://github.com/vn-tools/arc_unpacker)、
[KiriKiri2](https://github.com/krkrz/krkr2)、[ONScripter](https://github.com/ogapee/onscripter)。

136 个测试全部用**合成样本**跑（按格式规范逐字节现造），不依赖任何真实游戏——
所以你在 CI 上看到的绿灯，是真的在验证解析器，不是"文件存在就算过"。

用着有问题、或者你的游戏认不出来，欢迎开 issue，**把引擎和目录结构说一下**就行。
要是这个工具帮你省下了点时间，点个 ⭐ 就是最好的鼓励。

> 仅供个人学习、研究与翻译使用。请在自己合法拥有的游戏副本上使用，
> 不要传播提取出的原始资源。**游戏文本版权归原厂商所有。**

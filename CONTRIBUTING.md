# 贡献指南

感谢你愿意为 **GalText Extractor（GAL 文本提取器）** 出一份力。
本文档说明本地开发、测试与提交改动的约定，请先读完再动手。

---

## 开发环境

```bash
git clone https://github.com/Huadiaole/galtext-extractor.git
cd galtext-extractor
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -e ".[dev]"
```

- 要求 Python **3.10 及以上**（开发环境用的是 3.14）。
- `dev` 这个 extra 只装了 Pillow，用来重新生成图标；**运行程序本身一个第三方包都不需要**。
- 想要可选的 Fluent 风格主题再装 `pip install -e ".[modern]"`（sv-ttk），装不上也不影响功能。
- 界面调试直接跑 `python -m galtext gui`（或 Windows 下双击 `run_gui.bat`）。
- 手头没有能测的游戏目录时，先跑 `python make_sample.py` 生成一份**合成**样本再玩。

---

## 跑测试

```bash
python run_tests.py
```

- 入口固定是仓库根目录的 `run_tests.py`，内部用 `unittest` 发现 `tests/test_*.py`。
- 全部测试都应当通过；提交前请本地跑一遍（CI 会在 Linux + Windows、
  Python 3.10 / 3.12 / 3.13 上重跑同样的命令）。
- 测试数量随版本增长（1.1.0 时为 106 个），具体以输出里的 `Ran N tests` 为准。
- 也可以只跑单个模块：

```bash
python -m unittest tests.test_textkit -v
```

- 新增功能请**同时补测试**；纯文档改动可以说明原因后跳过。

### 测试哲学

**用合成样本按格式规范逐字节现造，不依赖真实游戏。**

`tests/fixtures.py` 就是照这个思路写的：XP3 封包、NSA/SAR 归档、BGI arc、
Shift-JIS 脚本等等，都是在内存里按格式规范拼出来的字节串。
这样做的好处是仓库里永远不会出现受版权保护的游戏数据，测试也能在任何机器上稳定复现。
新增解析器时请沿用这个做法，**不要**把真实游戏文件放进仓库。

---

## 代码规范

- 遵循 **PEP 8**，行宽上限 **100**（ruff 的 `line-length`）。
- 静态检查配置在 `pyproject.toml` 的 `[tool.ruff]`；提交前最好跑一次 `ruff check .`。
- 注释和文档字符串用中文，写清楚**「为什么」而不是「做什么」**：
  代码本身已经说明了「做什么」，注释应该记录约束、反例、格式怪癖和踩过的坑。
  例如：

  ```python
  # 这里的 xref 偏移是相对文件头之后的，不是文件绝对偏移；
  # 少加 header_len 会让 PDF 阅读器直接判定文件损坏。
  ```

- **不引入运行时依赖。** 只能用标准库（`tkinter` / `zlib` / `struct` / `re` /
  `pathlib` / `csv` / `json` 等），`[project] dependencies` 必须保持 `[]`。
  确实需要第三方包时，只能放进 `dev` / `modern` 这类可选 extras，并且要有「不装也能跑」的降级路径。
- 面向用户的文案（界面提示、错误信息、CLI 帮助）用中文；变量名、函数名、模块名用英文。

---

## 提交信息规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：`<type>: <简短描述>`，
描述用中文即可。常用 type：

| type | 用途 |
| --- | --- |
| `feat` | 新功能、新解析器、新导出格式 |
| `fix` | 缺陷修复 |
| `docs` | 文档、注释、README |
| `test` | 测试与样本构造 |
| `refactor` | 不改变行为的重构 |
| `chore` | 构建脚本、CI、工程配置、依赖 |

示例：

```
feat: 新增 NScripter SAR 归档解析
fix: 修正 xref 偏移漏算文件头长度导致 PDF 打不开
docs: 补充翻译模板的使用说明
test: 为 BGI arc 加压包补一个截断样本
refactor: 把编码判定统一收进 textkit
chore: 加上 GitHub Actions 测试矩阵
```

一次提交只做一件事；提交信息说清「改了什么、为什么改」。

---

## 分支与 PR 流程

1. 从 `main` 切出分支，命名建议 `feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 在本地改完并跑通 `python run_tests.py`。
3. 在 `CHANGELOG.md` 的 `[Unreleased]` 下补一条（面向使用者的描述，不是 commit log 复制）。
4. 推送到你的 fork，向 `main` 开 PR，按 PR 模板填写改动内容、自测情况和检查清单。
5. CI（`.github/workflows/ci.yml`）通过后由维护者 review；有界面变化请附改前 / 改后截图。
6. 合并方式以 squash 为主，保持历史干净。

---

## 如何新增一个引擎解析器

架构上「一个引擎一个模块」，新增引擎**只需要一步**：

1. 把模块放进 `galtext/parsers/`（例如 `galtext/parsers/myengine.py`）。

就这些。注册表通过 `pkgutil.iter_modules` **自动发现**该目录下的所有模块并逐个导入，
不需要在任何清单里登记。某个模块导入失败只会记录日志并标记为「不可用」，
不会影响其它引擎。

模块的展示与尝试顺序由 `galtext/parsers/__init__.py` 里的 `PREFERRED_ORDER` 决定：
在内置引擎之后按名字排序。**不在这个元组里的模块一样会被发现**，
只是排在后面 —— 它只影响顺序，不影响是否生效。

加完之后，`galtext engines`、引擎探测、命令行 `--engines` 和界面里的
「支持的解析器」就会同时看到它。想做「专业引擎」（有真实封包/字节码解析）还是
「通用模块」（散装脚本、兜底扫描），按就近的现有模块照抄即可。

必须实现的契约（与 `galtext/common.py` 中的约定一致）：

```python
ENGINE_ID = "myengine"
ENGINE_NAME = "My Engine"
def detect_dir(root) -> int          # 0-100 置信度
def iter_scripts(root)               # 产出 (virtual_path, data)
def extract_lines(vpath, data) -> list   # 产出候选文本行
```

要点：

- `ENGINE_ID`：内部标识，命令行 `--engine` 用它，必须稳定、只用小写字母。
- `ENGINE_NAME`：给人看的名字（可以带中文，例如 `"NScripter / ONScripter"`）。
- `detect_dir(root) -> int`：给目录的置信度打分，**0 表示「这不是我的游戏」**，
  100 表示「非常确定」。宁可打低分也不要在不认识的目录上返回高分，
  否则会把更合适的解析器挤掉。多个解析器同时命中时按分数从高到低依次尝试。
- `iter_scripts(root)`：产出 `(virtual_path, data)`；封包内文件也给出
  `virtual_path`（用 `/` 分隔的虚拟路径），解析失败的文件请跳过而不是抛异常。
  也可以产出 `(virtual_path, data, container)` 三元组来标注来源容器。
- `extract_lines(vpath, data) -> list`：只负责「把脚本里的字符串捞出来」，
  返回候选行（`str`，或 `(speaker, text)` 元组）。
  **不要**在这里做去重、长度过滤、标签清理——这些收尾工作由 `textkit` 统一完成，
  这样各个引擎的输出风格才会一致。
- 可选：`def guess_encoding(vpath, data) -> str`，用来覆盖自动编码判定
  （例如脚本头部自带编码声明）。返回 `"auto"` 表示交回默认逻辑。
- 可选的额外关键字参数（如 `encoding=` / `options=`）会被自动探测并按需传入，
  保持默认签名最省事。

自查清单：

- [ ] 合成样本能跑通（照 `tests/fixtures.py` 的风格按格式规范逐字节构造，别用真实游戏文件）。
- [ ] 对不相关目录返回 0 分，不抢别的引擎的活。
- [ ] 截断 / 损坏的文件只会被跳过，不会让整个扫描抛异常。
- [ ] 没有新增运行时依赖。

---

## 行为准则

- 保持友善与耐心：维护者和其他贡献者都是业余时间在做这件事。
- 就事论事地讨论技术问题，不攻击个人，不刷屏催更。
- **不接受求游戏资源 / 求汉化补丁的 issue 或 PR。**
  本仓库只提供解析与导出代码，不附带、不索引、不分发任何游戏本体、补丁或汉化文本。
- 提交的内容必须是你自己写的，或者是许可兼容的开源代码，并遵守 `LICENSE`（MIT）。

# 更新日志

本项目的所有重要改动都会记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.1.0] - 2026-09-25

### Added

- **跨行说话人跟踪**：识别 `[name text="…"]`、`[chara_mod name=…]` 这类
  「当前说话人」声明并沿用到后续台词，解决「台词行本身不带名字」时人名丢失的问题。
  可用「跟踪说话人声明」开关（`--no-speaker-tracking`）关闭。
- **「自动识别单独成行的角色名」**：支持名字与台词分行的脚本写法。
  启发式，默认关闭（`--guess-speakers`）—— `翌日` 这类短旁白和名字长得一样。
- **「旁白标记」**：填 `旁白` 可让每行都带名字，txt / CSV / JSON / 剧本 PDF 都会带上
  （`--narration-label`）。
- 扫描结束时会提示「有多少条文本含假名」，让残留的日文原文可见，
  并指明用「只保留中文行」过滤。
- **封包覆盖语义**：同一脚本在多个封包中各有一份时（例如日文原版 `data.xp3`
  与汉化补丁 `patch.xp3` 装着同名脚本），只保留优先级最高的那份，
  被覆盖的会列进报告并附汇总提示。可用「封包覆盖」开关或 `--keep-all-versions` 关闭，
  以便对照原文与译文。
- **「只保留中文行」过滤**（`--chinese-only`）：按假名滤掉日文原文行，
  用于补丁形式特殊、覆盖语义失效时兜底。
- **剧本风格 PDF 导出**：自带零依赖 PDF 写入器，无需 ReportLab 之类的第三方库，
  自动挑选系统中文字体并做字形子集化，输出可直接阅读 / 打印的剧本版式。
- **TrueType / TTC 字形子集化**：只嵌入实际用到的字形，
  9.7 MB 的字体文件最终只占 35–220 KB，导出的 PDF 体积可控。
- **界面主题系统**：配色 / ttk 样式 / 图标集中在 `galtext/theme.py`，
  支持明暗切换（`GALTEXT_MODE` 环境变量可指定初始模式）；
  检测到 `sv-ttk` 时自动升级为 Fluent 风格，否则使用内置自绘主题 —— 两条路都不引入必需依赖。
- 应用图标（PNG + 多尺寸 ICO）与 12 个工具栏图标，由 `scripts/make_assets.py` 生成。
- 开源工程文件：LICENSE、GitHub Actions CI、issue / PR 模板、CONTRIBUTING、SECURITY 等。
- 测试从 83 个增加到 110 个，新增主题、界面资源与解析器自动发现用例。

### Changed

- 界面全面美化：品牌栏、配色、间距、控件样式、空状态提示。
- 导出入口统一，剧本 PDF 与其它格式走同一套 API。

### Fixed

- 修正 xref 偏移漏算文件头长度，导致生成的部分 PDF 无法打开的问题。
- 修正导出时的编码判定。
- 修正通用二进制扫描在自动模式下 `include_generic` 未生效的问题。
- 修正文本预览里「说话人 / 旁白」配色标签从未被应用、导致整段同色的问题。
- 修正 `pyproject.toml` 的 `package-data` 未包含图标子目录，
  导致 pip 安装后工具栏图标丢失的问题。
- 修正自绘主题下勾选框被渲染成叉号的问题（改为自绘指示器图片）。
- 修正打包后的 GUI exe 无法接收目录参数、以及 `--windowed` 构建缺少控制台时的异常行为；
  打包改为同时产出 GUI 版与 CLI 版两个 exe。
- 解析器注册改为真正的自动发现（`pkgutil` 扫描 `parsers/`），
  与文档承诺的「丢一个模块进去即可」保持一致。

## [1.0.0] - 2026-09-24

### Added

- 首个版本。
- 引擎探测与五种解析器：KiriKiri XP3、NScripter NSA / SAR、BGI arc、散装脚本、通用二进制扫描。
- 编码自动探测：Shift-JIS / GBK / Big5 / UTF-8 / UTF-16（含无 BOM）。
- 文本清洗与说话人拆分。
- Tkinter 图形界面（扫描 / 预览 / 搜索）。
- 命令行入口 `python -m galtext`（`scan` / `engines` / `gui`）。
- 导出 TXT / CSV / TSV / JSON / JSONL / 翻译模板。

[Unreleased]: https://github.com/Huadiaole/galtext-extractor/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Huadiaole/galtext-extractor/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Huadiaole/galtext-extractor/releases/tag/v1.0.0

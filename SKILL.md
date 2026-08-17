---
name: paper-notes
description: "Use when creating or managing literature items in the Obsidian vault (Obsidian-native paper-notes). MinerU PDF→MD + Figure解读 + 文献卡片. N<2 main agent; N≥2 auto parallel joinable workers. Triggers: 导入文献/用minerU转换/转为md笔记/Figure解读/文献卡片/建条目/Zotero迁移."
license: MIT
---

# paper-notes（Obsidian-native 文献系统：PDF → Obsidian MD + Figure 解读）

Obsidian-native 文献管理系统：以 vault 内 portable Markdown/YAML 与文件为持久资产，`paper-notes` CLI 是唯一受管写入者，Hermesian/本 skill 负责 MinerU 转换与 Figure 解读生成。**不再要求 Zotero 运行时**：过渡期 Zotero 仅作只读来源，迁移后新文献完全不经 Zotero；任何笔记都不写 active `zotero://` 链接（旧链接由 migration 移除）。

## 前置条件

- Python 3.11+ + `requests` + `PyMuPDF`（fitz，见 `requirements.txt`）
- MinerU API Token（`MINERU_TOKEN` 环境变量或 `--token`）
- Obsidian vault（`05 Literature/` 为文献根）
- `paper-notes` CLI（`python3 -m paper_notes.cli`，见 `references/cli_protocol.md`）
- （过渡期可选）运行中的 Zotero 仅用于 `migrate legacy-obsidian` 发现既有条目

## Canonical 目录布局（唯一当前布局）

```text
05 Literature/<citation_key>/
├── <citation_key>.md      ← 主条目（唯一权威书目记录，必选）
├── <citation_key>.pdf     ← canonical 主 PDF（被选定的主 PDF）
├── minerUmd_<citation_key>.md
├── Figure解读_<citation_key>.md
├── attachments/           ← 补充 PDF 与 supplementary 文件
├── cards/                 ← 仅本论文派生的卡片
└── figures/               ← 最终高清 Figure 资产
    └── <sha256>.png
```

- 目录名 == 主文件名 == `citation_key`（vault 全局唯一）；`paper_id` 是永久机器身份，重命名 key 不改变它。
- `<citation_key>.md` 是唯一权威（authoritative）书目记录：书目字段、`pdf_status`、`reading_status` 只在这里；派生笔记只保留最小关系字段（`paper_id` / `citation_key` / `paper` wikilink）。规范见 `references/frontmatter_spec.md`。
- **禁止** volatile 指标字段（EasyScholar / IF / JCI / JCR / CAS partition）——绝不写入任何 Markdown，只在插件 UI 展示（见「EasyScholar」节）。
- 常规流程**不删除** `<citation_key>.pdf`（canonical 主 PDF 是永久资产；`item delete` 是显式、确认制的永久删除，不在此列）。
- `attachments/` 放补充 PDF 与 supplementary 文件；`cards/` 只放由本论文**派生**的卡片；`figures/` 放该篇**最终**高**清** Figure 资产（content-addressed `![[<sha256>.png]]`，minerUmd 与 Figure解读 引用同一 embed）。
- 跨论文综合笔记留在全局位置，不进任何论文目录。

## 新建文献条目（CLI）

创建、更新、附加 PDF、重命名 key、删除全部走 CLI（JSON envelope 协议）：

```bash
cd <paper-notes-repo> && python3 -m paper_notes.cli item create \
  --vault "<vault>" --doi "10.xxxx/xxxx" [--pmid ... --pmcid ... --arxiv ... --url ... --pdf "..."]
```

- 从结构化来源（PubMed/Crossref/arXiv 等）取元数据；冲突或关键字段缺失 → `needs_confirmation`（候选值），用户确认后 `--confirmed <json>` 重跑。
- `item attach-pdf` 把主 PDF 复制进 `<paper_dir>/<citation_key>.pdf`（SHA-256 校验；源文件不移动不删除）；非主 PDF 进 `attachments/`。
- `item reconcile` 修正 `pdf_status` 与磁盘不一致；`item rename-key` / `item delete` 先 dry-run 影响清单再确认，事务执行。
- 命令面、envelope 字段与退出码见 `references/cli_protocol.md`。

## MinerU 转换（Hermesian + paper-notes 流程）

MinerU 转换与 Figure 解读生成**始终是 Hermesian + paper-notes skill 工作流**（Obsidian 插件从不启动它们）。

**小 PDF（≲8MB）**：

```bash
python3 <repo>/scripts/mineru_upload.py \
  "<vault>/05 Literature/<citation_key>/<citation_key>.pdf" \
  "<vault>/05 Literature/<citation_key>/" \
  --language en --citation-key <citation_key>
```

带 `--citation-key` 时清洗后的笔记直接定稿为 `minerUmd_<citation_key>.md`（不带则保持旧 `full.md` 名）；输出目录**绝不允许**是 `figures/`。

**大 PDF / 代理上传卡住**：按 `references/mineru_upload_proxy.md` 分步（压 ~120dpi JPEG → `file-urls/batch` → `curl PUT` → 轮询 → `--noproxy '*'` 下载）；多篇同批用 `references/mineru_multi_file_batch.md`；CDN 下载失败批量恢复见 `references/bulk_cdn_recovery.md`。

**下载失败处理**：脚本内置 `curl` 回退；仍失败用 `--download-only <batch_id>`。MinerU 原始图片（`images/`）只作定位线索（识别 Figure 归属与页面），低像素 JPG 绝不作为最终插图。

## 清洗 MD

```bash
python3 <repo>/scripts/clean_md.py "<vault>/05 Literature/<citation_key>/minerUmd_<citation_key>.md" \
  --in-place --attachments-dir "<vault>/05 Literature/<citation_key>/attachments/"
```

canonical 调用把 MinerU 图片迁移进该篇 `attachments/` 为 Obsidian embeds（仍只作定位线索）；`--attachments-dir` 指向 `figures/` 一律拒绝。旧调用（不带 `--attachments-dir`）保持删除图片引用。清洗内容：图片引用处理、`<details>`/`<table>` HTML 块、单字母面板标签、`(legend continued)` 行、`## Figure` → `**Figure ...**`。非零退出 = 整体失败、MD 未动。

## 高清 Figure 渲染（canonical 主 PDF → 300dpi PNG）🚨 图片唯一来源

所有最终插图**必须**来自该篇 canonical 主 PDF（`<paper_dir>/<citation_key>.pdf`），用 `scripts/render_pdf_figure.py` 渲染 ≥300 dpi 无损 PNG 到 `<paper_dir>/figures/`；MinerU JPG 一律不作为最终插图。

对每张主图/补充图（顺序按 minerUmd 图注顺序）：

1. **定位 caption 页**：在 canonical 主 PDF 中搜索该图图注文本（如 `Figure 1.` / `Fig. S1`）确定页码（1-based）
2. **渲染临时页面预览**：先用 `render_pdf_figure.py` 以整页 bbox 渲染到 `/tmp`（或 PyMuPDF 缩略图），供视觉确认
3. **视觉确认完整整图 bbox**：对照 caption 与正文，确认 bbox 恰好框住**完整整图**（含全部 panel 与子标签，不含 caption 文本、不含正文）
4. **渲染 300dpi PNG**：

```bash
python3 <repo>/scripts/render_pdf_figure.py \
  "<vault>/05 Literature/<citation_key>/<citation_key>.pdf" \
  --page <caption 页码> --bbox <x0>,<y0>,<x1>,<y1> \
  --dpi 300 --output-dir "<vault>/05 Literature/<citation_key>/figures"
```

   输出文件名 = PNG 字节 SHA256（`<64位hex>.png`，同内容幂等复用）；stdout 为单行 JSON（`path`/`embed`/`width`/`height`/`page`/`bbox`/`dpi`）。**任何校验失败 → 非零退出且输出目录零写入**。
5. **嵌入 minerUmd**：把 `![[<64位hex>.png]]` 插到对应 Figure legend **之前**（同一 PNG 后续 Figure解读 直接复用同一 embed）

**每图只放一张完整整图**，禁止把 panel/图标碎片堆进笔记；某图无法从主 PDF 可靠定位完整整图时，该位置明确写「*原 PDF 未能可靠定位完整 Figure*」，**不得猜裁**。渲染失败（页码/bbox 越界、PDF 损坏）→ 检查主 PDF 路径与页码后重试，仍失败则写不可靠声明。此步骤**不删除** `<citation_key>.pdf`。

## 创建 Figure解读笔记 🚨 质量关键

这是整个流程中**最需要人工质量把关**的步骤。**不要走捷径生成占位笔记**——每张 Figure 必须逐 panel 精读。

**也可独立触发**（目录里已有 `minerUmd_<citation_key>.md`）：「Write complete Figure解读…from minerUmd」「补全 Figure解读」——跳过前序步骤，直接做本步（渲染步骤见 `references/figure_interpretation.md`「独立任务」）。

1. **读取** `<vault>/05 Literature/<citation_key>/minerUmd_<citation_key>.md` 全文（含 Methods 里对 fig. S 的引用）
2. **通读正文**，理解每张 Figure 对应的叙事逻辑和实验方法
3. **图注补全**：minerUmd 图注残缺时用 canonical 主 PDF（PyMuPDF）抽 Fig legend；Science 类主 PDF 常无 S1–Sn 全文图注——补充图按正文/Methods 逐图归纳并**声明非逐字图注**，禁止空图或伪造出版社图注
4. **按 `references/figure_interpretation.md` 格式**，为每张 Figure 撰写原文 legend + 四维解读
5. **图片嵌入（第一轮生成时）**：每张主图/补充图都在对应 `## Figure X/SX` 标题**之后、叙事之前**嵌入高清整图——`![[<64位hex>.png]]`（300dpi 无损 PNG，与 minerUmd 中**同一文件、同一 embed**，由 `scripts/render_pdf_figure.py` 从 canonical 主 PDF 渲染并落入该篇 `<paper_dir>/figures/`）。**每图只放一张完整整图**，禁止把 panel/图标碎片堆进笔记；无法可靠定位完整 Figure 时明确写「*原 PDF 未能可靠定位完整 Figure*」，不得猜裁。
6. **Main figures**：逐 panel，原文 legend 在 `>` 引用块中，解读用 `- **是什么/为什么/怎么做/发现**`
7. **Supplementary figures**：**与主图相同格式**——逐图 `## Figure S…`＋图片嵌入＋`> **叙事位置**`＋逐 panel `**Panel X**` / `>` 原文 Legend / `- **是什么/为什么/怎么做/发现**`；S 图很多时可按主题分 `###` 块。缺出版社 legend（Science 类主 PDF 常无 S 图全文图注）时仍逐图排版，并在该图下标明「据正文/Methods 归纳，非出版社逐字图注」。
8. **「怎么做」增强**：湿实验须注明实验模型；干实验须注明数据来源（如 scVI/milopy/cell2location + 细胞数）；不确定就写不确定——详见 `references/figure_interpretation.md`
9. **「发现」字段**：核心结果/结论，不复读「是什么」——详见同文件
10. **不要删除原文 legend**，必须和解读合并在一起
11. **Frontmatter**：最小关系字段 `paper_id` / `citation_key` / `paper: "[[<citation_key>]]"`；**不要**添加 `tags`、`type`；阅读状态只存在于主条目（`reading_status`）
12. **落盘**：`terminal` + Python `Path.write_text`；勿把中文 vault 路径设为 `terminal` 的 `workdir`

保存为：`<vault>/05 Literature/<citation_key>/Figure解读_<citation_key>.md`

```yaml
---
paper_id: <uuid>
citation_key: <citation_key>
paper: "[[<citation_key>]]"
---
```

**文首 `## Overview`（强制）**：frontmatter 之后、第一张 Figure 之前必须有 `## Overview` 节——4–6 条 bullet，每条以 `**维度名**：` 开头（中文），覆盖：研究背景/问题、研究脉络（问题→思路→分析主线）、主要方法、关键结论/意义。目标是读者看完 Overview 即理解全文脉络、背景与主要方法；不要写成段落。（详见 `references/figure_interpretation.md`）

## 文献卡片（cards/）

从已有 Figure解读 拆分卡片**首选 CLI**（`paper-notes card create`），落该篇 `<paper_dir>/cards/`；卡片只派生自本论文，frontmatter 用最小关系字段（`paper_id` / `citation_key` / `paper` wikilink）。

```bash
cd <paper-notes-repo> && python3 -m paper_notes.cli card create \
  --vault "<vault>" --key <citation_key> \
  --title "<结论性的一句话>" \
  --selection-file /tmp/selection.md \
  [--filename card_Figure2_MyCard.md] \
  --anchor-name fig2-interpretation --source-note "Figure解读_<citation_key>" \
  [--backlink]
```

- `--selection-file`：选中内容的 verbatim Markdown（图片 embed、legend、四维解读原样）。
- 默认文件名 `card_<slug>.md`（slug 由 title 生成，保留中文）；`--filename` 可覆盖。
- 提供 `--anchor-name` + `--source-note` 时：CLI 在源 Figure解读 笔记的选区末尾幂等插入 `^anchor`，并在卡片中生成 `> 参见 [[Figure解读_<key>#^anchor|...]]` 回链。
- **双链铁律**：创建卡片默认加 `--backlink`——CLI 在源笔记 anchor 行之后插入 `> 卡片：[[<card>]]`，使 Figure解读 → 卡片 也可见地可跳转（加上卡片内的 `参见` 回链即构成显式双向链接；Obsidian 的 Backlinks 面板会自动追踪两侧）。回链幂等，已存在则 `card_warning`。
- 目标卡片已存在 → `conflict`（退出码 3，零写入）；源笔记无法定位选区末尾 → `card_warning` 但卡片仍创建。
- 执行层薄封装见 `literature-card-from-figure-notes` skill；它负责读取选区、生成结论性标题，然后调用本 CLI。

## Library UX（Obsidian 插件交互）

Obsidian 插件提供文献库（Library）表格视图与 Detail Drawer（右侧详情抽屉）。这些交互由插件 UI 实现，回答相关操作问题时按此描述：

- **Row Activation（行激活）**：**单击行** = 选中该行并打开 Detail Drawer（只读详情 + 操作）；**双击行** = 只打开 Primary PDF（`<citation_key>.pdf`）；该行**缺 PDF 时双击仅弹 Notice**——不开 Drawer、不开 Figure 笔记。
- **Open Folder**：Detail Drawer 内按钮；在 **Obsidian 应用内文件 explorer** 中 reveal Canonical Paper Directory（`05 Literature/<key>/`），explorer 未开则打开；**不是 macOS Finder**。
- **Reading Status Cycle（阅读状态循环）**：表格单元格与 drawer header 的 reading chip 均可点击；点击循环 `unread → reading → read → unread`，经 CLI `item update` 写入主条目 frontmatter `reading_status`。**Chip-Local Click**——chip 点击只循环状态，不冒泡为行激活（不打开/切换 Drawer）；action bar 的 `Reading: x → y` 快捷按钮已移除，不再使用。
- **Journal Metrics（期刊指标）**：见下节「EasyScholar」——CAS/JCR/IF/JCI 为 volatile UI-only，只在插件 UI 展示（列徽章 + drawer 段），绝不写入任何 Markdown。

## EasyScholar（UI-only：绝不写入 Markdown）

Journal Metrics（CAS/JCR/IF/JCI）是 **volatile UI-only 数据（仅插件界面展示：列徽章 + Detail Drawer 段）**，**唯一来源是 EasyScholar**（与 Zotero zotero-style / Ethereal Style 插件**同源数据族**），**绝不写入**任何 Markdown（不写入主条目、派生笔记、卡片与索引）。SecretKey 存放在 vault 外私有配置（`~/Library/Application Support/paper-notes/config.json`，0600），插件调 CLI 查询、不显示不记录密钥；CLI 提供 `metrics query` 与 `config easyscholar`（一次性从旧 Zotero 配置导入需显式确认，不打印值）。缓存 30 天、失败保留旧值并标记 stale，绝不影响检索/引用/导出。

## 迁移（legacy Obsidian → canonical）

库内既有旧布局目录（论文全标题 + 旧 frontmatter `citation key`/`zotero`/`zotero link`/`状态`、旧高清图目录 `<paper_dir>/Figure_<paper_title>/`）用 CLI 分批迁移：

```bash
python3 -m paper_notes.cli migrate legacy-obsidian --dry-run   # 只读计划
python3 -m paper_notes.cli migrate legacy-obsidian --apply <run_id>
python3 -m paper_notes.cli migrate verify <run_id>
python3 -m paper_notes.cli migrate rollback <run_id>
```

- 每次迁移有 `run_id`；备份/manifest/journal 在 vault 外 `~/Library/Application Support/paper-notes/migrations/<run_id>/`，**永不自动删除**。
- 迁移移除旧 `zotero://` 依赖与重复状态字段（`状态` → 主条目 `reading_status`）；**绝不修改或删除** Zotero 数据库/存储；迁移后不再写入 active `zotero://` 链接。
- 幂等：重复运行同一迁移一致；不一致停止交人工复核。完整生命周期见 `references/migration.md`。

## CLI 协议（--json）

所有 CLI 命令支持 `--json`：stdout 恰好一个 versioned envelope（`protocol_version: 1`，`status: success|needs_confirmation|conflict|error`，`data`，`warnings[]`，`errors[]`），人类诊断只走 stderr；退出码 0/2/3/4。详见 `references/cli_protocol.md`。

## 批量导入（去重后 N 决定）

当用户要求导入多篇文献时：

- 已存在的论文自动跳过（检查 `minerUmd_*.md` 与主条目是否存在），按待处理篇数记为 N
- **N < 2**：当前主 Agent 完整执行全部流程，不启动子 Agent
- **N ≥ 2**：自动为每篇启动一个独立 worker 并行执行，每个 worker 只处理一篇文献，禁止递归再分派

### MinerU 转换（N ≥ 2 时并行批处理）

对多篇 PDF，采用 **上传全部 → 统一轮询 → 统一下载**：同一 `file-urls/batch` 挂多文件（单 `batch_id`）→ 顺序 curl PUT（走代理）→ GET 统一轮询 → 各 zip `curl --noproxy '*'`。可复制步骤见 `references/mineru_multi_file_batch.md`；代理/压缩兜底见 `references/mineru_upload_proxy.md`。

### 并行生命周期与自动收尾（N ≥ 2）

主 Agent 始终是任务所有者。只允许采用可等待、可获取退出状态的 worker 机制，不依赖用户消息触发收尾。**推荐机制**：`hermes chat -Q --source tool -s paper-notes -q "Write complete Figure解读... from minerUmd at <path>"` 启动独立进程，由主 Agent 通过受管进程的 wait/join 等待完成。

1. 输入：每个 worker 收到一篇的 minerUmd 绝对路径、figure_interpretation.md 引用、Frontmatter 字段、中文撰写指令、输出路径
2. 输出：每个 worker 直接写出该篇的 `minerUmd_<citation_key>.md` 与 `Figure解读_<citation_key>.md` 到对应目录，并记录状态
3. 主 Agent 等待全部 worker 进入成功/失败终态，再逐篇读回验证
4. 所有 worker 结束前不发送最终答复
5. 无 wait/join 能力时自动退化为顺序执行
6. 个别文献失败不得阻塞已成功文献；最终一次性汇报成功、失败及原因

**Figure解读质量要求**（与单篇一致，不降级）：

- 每篇必须含 `**是什么**`、`**为什么**`、`**怎么做**`、`**发现**`
- 每张主图/补充图都须在 `## Figure X/SX` 标题后嵌入同一高清整图 `![[<64位hex>.png]]`（从 canonical 主 PDF 300dpi 渲染、落该篇 `<paper_dir>/figures/`，多图按原顺序；不可靠时写「原 PDF 未能可靠定位完整 Figure」，不得猜裁）
- 主文体量通常 >10KB
- 遵循 `references/figure_interpretation.md` 完整规范

## Topic MOC / 主题表填行（可选但常见）

导入后常写入 `05 Literature/MOCs/<主题>.md` 的 Topic Table（如「单细胞milo分析」），列固定 **Title | Figure解读 | 总结 | 卡片**：

1. **Title**：完整英文标题（与同文件其它表一致，不用 citekey）
2. **Figure解读**：`[[Figure解读_<citation_key>]]`（仅笔记名）
3. **总结**：2–3 条短 bullet，`<br>` 换行；**面向该主题的方法学要点**，非全文摘要
4. 用 `patch` 替换时把 `## 主题` + 表头 + 空行一并纳入唯一匹配块（编辑器选区限制时尤其重要）

**方法学主题表的 总结 铁律**：先在 minerUmd 检索方法名。真正跑了该 pipeline（Methods 写 milopy / KNN neighborhood DA）→ 写具体用法；**仅 References 引用、正文用 Wilcoxon/GLM 等** → 写清「引用 X；主文用 Y」，禁止写成「用 Milo 做了…」。

## 错误处理

| 场景 | 处理 |
|------|------|
| `item create` 返回 needs_confirmation | 展示候选值让用户确认，`--confirmed <json>` 重跑 |
| PDF 附加 SHA-256 不匹配 | 拒绝写入并报告；源文件不受影响 |
| MinerU 上传失败（签名错误） | 检查 token 是否过期 |
| MinerU 上传卡死 / 前台超时 | PDF 过大或代理限速 → 按 `references/mineru_upload_proxy.md` 压缩 + curl 分步上传 |
| MinerU 轮询超时 | 告知用户，保留 batch_id 可稍后手动查询 |
| MinerU 下载 SSL 失败 | 脚本内置 curl 回退；仍失败用 `--download-only <batch_id>`；CDN 优先 `--noproxy '*'` |
| URL extract task 失败 | 改走本地 PDF 上传，勿依赖期刊直链 |
| 批量 Figure解读 占位 | 🚨 必须走并行生命周期协议（N≥2 时 hermes chat -Q 或主 Agent 顺序执行），绝对不要 Agent 自己逐篇写 |
| Figure 渲染失败（主 PDF 打不开 / 页码或 bbox 越界 / dpi<300） | 检查 canonical 主 PDF 路径、caption 页码与 bbox；`render_pdf_figure.py` 非零退出且输出目录零写入，修复后重试；仍失败则写「原 PDF 未能可靠定位完整 Figure」 |
| 迁移冲突（目标已有不同内容） | 停止交人工复核；绝不覆盖不同内容 |
| YAML 非法 / 文件与元数据不一致 | 条目保持可见并给字段级诊断；`item reconcile` 提议修正；绝不静默覆盖人工编辑 |
| `card create` 目标已存在 | `conflict`（退出码 3）零写入；换标题/文件名重跑 |
| `card create` 源笔记定位失败 | `card_warning`：卡片已创建但 anchor 未插入，手动补 anchor 或去掉 `--anchor-name` |
| `card create` 回链已存在 | `card_warning`：`> 卡片：[[...]]` 已在源笔记中，幂等 no-op |

## 注意事项

- PDF > 200MB 或 > 200 页 → MinerU 不支持，需用户手动拆分
- 非英文文献需传 `--language ch`（中文）等参数
- Figure 数量过多（>14 张主图）时告知用户并确认
- 派生笔记 frontmatter 用最小关系字段；`tags`/`type` 一律不加
- 清理临时产物只允许针对 `mineru_extract/`、`*.zip`、`images/` 等——**绝不删除** `<citation_key>.pdf` 与 `figures/` 内容（canonical 主 PDF 与最终插图是永久资产）

## Gotchas（经验教训）

1. **Figure解读 是质量分水岭**——占位笔记 vs 完整解读是用户最能感知的质量差异。宁可慢，不降质。
2. **「发现」不是「是什么」的复读**——前者是结论（"亮氨酸最显著上调 HLA-DR"），后者是内容描述（"各氨基酸刺激下 HLA-DR⁺ 比例"）。如果混淆，整篇 Figure解读 会变成啰嗦的流水账。
3. **模型/来源不确定就写不确定**——编造的细胞系名或样本量比不写更糟糕。原文未说明模型时写「原文未明确说明」。
4. **MinerU CDN SSL + 本地代理冲突**——Python `requests` 对 `cdn-mineru.openxlab.org.cn` 常报 `SSLEOFError`。脚本内置 `curl` 回退。但若本地有 HTTPS 代理，curl 也会因代理隧道的 TLS 握手失败。**解法**：`curl --noproxy cdn-mineru.openxlab.org.cn -L -o <zip> <url>` 或 `unset HTTP_PROXY HTTPS_PROXY` 后重试。也可用 `--noproxy '*'` 全局绕过代理下载。
5. **MinerU 输出目录禁止指向 `figures/`**——临时解析产物绝不落最终 figure 资产目录；`mineru_upload.py` / `clean_md.py` 会直接拒绝。
6. **write_file 对中文路径静默失败**——Hermes `write_file` / `execute_code` 在路径含中文（如 `知识库`）时可能报成功但不落盘。**必须用 `terminal` + Python `Path.write_text`**。另：**`terminal` 的 `workdir` 不能含中文**（会 Blocked: disallowed character）——省略 workdir 或用英文 cwd，绝对路径写在 Python 字符串内。
7. **独立 Figure解读 ≠ 文献卡片**——`literature-card-from-figure-notes` 从已有 Figure解读拆卡片（落 `cards/`，核心写盘走 `paper-notes card create` CLI）；**写完整 Figure解读**走本 skill「创建 Figure解读笔记」/ `references/figure_interpretation.md`。
8. **SM 图注不在 minerUmd 里**——先扫 PDF 页数与是否含 `Fig. S`；仅有主文时用正文 `fig. Sx` + Methods 逐图归纳补充图 legend 并声明归纳来源，勿假装有完整 SM legend。
9. **Figure解读 提问须先查原文**——当用户针对 Figure解读 笔记提问（如"这个方法怎么做的""这个术语什么意思"），必须先查阅同目录下的 `minerUmd_*.md` 原文 Methods 部分，用原文的实际方法作答，而非依赖一般性推测或常见做法。教训：曾将 signature scoring 方法推测为 AUCell/H 矩阵，实为 Seurat AddModuleScore + 30-bin 对照基因方案（原文 Methods "Scoring gene sets" 明确写了公式）。先查原文再答，避免知识污染。
10. **OSS 上传在本地代理下极慢 / 易超时**——`mineru.oss-cn-shanghai.aliyuncs.com` 经本地 HTTPS 代理时实测约 15–40 KB/s。**优先**：PyMuPDF 压到 ~120dpi JPEG（**仅供 MinerU OCR，最终插图从 canonical 主 PDF 渲染**）再 `curl PUT --data-binary`（无 Content-Type、走代理、长超时）；见 `references/mineru_upload_proxy.md`。不要对 OSS 用 `--noproxy '*'`（本环境曾卡在 Expect:100）。
11. **URL 提取 API 不可作期刊 PDF 主路径**——`POST /api/v4/extract/task` + `{"url":...}` 对 Cell/PMC/DSpace 等常 `failed to read file` 或拉到 HTML。优先本地 canonical PDF。
12. **高清 Figure 一律从 canonical 主 PDF 渲染**——MinerU JPG（常 270×284，低像素）只作定位线索，清洗时迁移为 `attachments/` embeds 或删除引用，绝不作为最终插图、绝不进入 `figures/`；旧版 `01 attachments` 附件区（legacy）已由 canonical `attachments/` 取代。最终插图用 `scripts/render_pdf_figure.py` 以 300dpi 从主 PDF 裁完整整图，content-addressed `![[<64位hex>.png]]` 落该篇 `<paper_dir>/figures/`，minerUmd 与 Figure解读 复用同一 embed。
13. **Figure legend OCR 残缺**——压缩 PDF 仅供 MinerU OCR，部分 panel 图注可能丢失；禁止空 panel 占位：用 RESULTS 正文补齐，并在引用块注明「（图注 OCR 不完整，据正文归纳）」。压缩不影响最终插图（插图从 canonical 主 PDF 渲染）。
14. **批量 PUT 顺序优于并行**——同批多个 OSS URL 在本地 HTTPS 代理下并行上传易 thrash；顺序 PUT 更稳。
15. **主题总览表 ≠ 全文摘要**——填「milo/NMF/…」类表时，总结只写与该主题相关的分析设计；文献仅引用方法学原文时不要写成「使用了该方法」。
16. **迁移只动 vault**——`migrate` 绝不修改/删除 Zotero 数据库与存储；备份在 vault 外，rollback 前先 `verify <run_id>`。

## 参考文件

- `references/frontmatter_spec.md` — 主条目 schema、目录语义与派生笔记 frontmatter
- `references/figure_interpretation.md` — Figure 四维解读格式（Overview、整图嵌入、panel 规则、模型/来源标注）
- `references/cli_protocol.md` — CLI 命令面与 JSON envelope 协议
- `references/migration.md` — legacy Obsidian 迁移 run_id 生命周期
- `references/mineru_upload_proxy.md` — 大 PDF / 代理环境下的 MinerU 分步上传与下载
- `references/mineru_multi_file_batch.md` — 单 batch 多文件 file-urls 上传 + 统一轮询/下载
- `references/bulk_cdn_recovery.md` — 多篇论文 CDN 下载同时失败时的批量恢复脚本模板
- `scripts/render_pdf_figure.py` — 从 canonical 主 PDF 渲染 300dpi 无损 PNG 整图到 `<paper_dir>/figures/`（测试见 `tests/test_hires_figure_pipeline.py`、`tests/test_figure_directory_policy.py`、`tests/test_v2_figure_directory_policy.py`）

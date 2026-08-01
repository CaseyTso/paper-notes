---
name: paper-notes
description: "Use when importing Zotero literature to Obsidian notes (paper-notes). MinerU PDF→MD + Figure解读. N<2 main agent; N≥2 auto parallel joinable workers. Triggers: 导入文献/用minerU转换/转为md笔记/Figure解读/导入Zotero合集."
license: MIT
---

# paper-notes（Zotero PDF → Obsidian MD + Figure 解读）

用 MinerU 精准解析 API 将 Zotero 中的 PDF 文献转为结构化 Obsidian Markdown 笔记，清洗后生成 Figure 解读笔记。全程自动化：查 Zotero → 取 PDF → MinerU 转换 → 清洗 → 命名 → 解读。

## 前置条件

- MinerU API Token（需用户提供，或已设 `MINERU_TOKEN` 环境变量）
- Zotero 应用运行中（内置 MCP 在 `http://127.0.0.1:23120/mcp`）
- Python 3 + `requests` + `PyMuPDF`（fitz）库（见 `requirements.txt`；`scripts/render_pdf_figure.py` 渲染高清 Figure 需要）

## 输出目录

默认输出到 vault 的 `05 Literature/<paper_title>/`。`<paper_title>` = 文献英文全标题消毒后的目录名（规则见 `references/frontmatter_spec.md` 的「paper_title 生成」），**不截短**。

每篇的最终高清 Figure PNG 统一输出到 `<paper_dir>/Figure_<paper_title>/`（`<paper_dir>` = `<vault>/05 Literature/<paper_title>/`；`Figure_<paper_title>` 中的 `<paper_title>` 与论文目录使用**同一套**已消毒英文全标题，**不截短**）。minerUmd 与 Figure解读 两篇笔记都引用该目录内的同一 `![[<64位hex>.png]]`。

Figure解读 将原 `source` 拆成两个**顶层**属性：`minerU`（wikilink）与 `zotero link`（URI），禁止嵌套在 `source:` 下；文首强制 `## Overview`（规格见 `references/figure_interpretation.md`）。

## 单篇处理流程

### Step 1：定位 Zotero 条目

Zotero MCP 通过 HTTP JSON-RPC 调用。所有请求为 POST `http://127.0.0.1:23120/mcp`。

**按标题搜索**：
```bash
curl -s -X POST http://127.0.0.1:23120/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_library","arguments":{"q":"<title terms>","mode":"preview","limit":5}}}'
```

**按合集批量**：
```bash
curl -s -X POST http://127.0.0.1:23120/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_collection_items","arguments":{"collectionKey":"<key>","limit":50}}}'
```

**获取条目详情**：
```bash
curl -s -X POST http://127.0.0.1:23120/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_item_details","arguments":{"itemKey":"<key>","mode":"standard"}}}'
```

结果在 `result.content[0].text`（JSON 字符串，需二次解析）。

提取：
- `key` — 条目 ID
- `title` — 论文标题
- `attachments[].key` — PDF attachment key（取 `contentType: "application/pdf"` 的那个）
- `attachments[].filename` — PDF 文件名

### Step 2：获取 citation key

Citation key 存储在 Zotero SQLite 的 `citationKey` 字段中（Better BibTeX 生成）。

**单篇**：
```bash
python3 ~/.hermes/skills/research/paper-notes/scripts/get_citekey.py <item_key>
```

**批量（推荐）**：一次查询所有 key 避免 DB 锁定：
```bash
echo -e "KEY1\nKEY2\nKEY3" | python3 ~/.hermes/skills/research/paper-notes/scripts/get_citekey.py --batch
```
输出为 JSON：`{"KEY1": "citekey1", "KEY2": "citekey2", ...}`

如果条目没有 citation key，脚本会 fallback 从 title + year 生成。结果记为 `<ckey>`。

### Step 3：准备输出目录 & 复制 PDF

文件夹名 = 文献英文全标题（消毒后），记为 `<paper_title>`。消毒规则：先剥 HTML 上标（`<sup>+</sup>`→`+`），再 `/`→`-`、去掉 `<>:"|?*\`、压缩连续空白、去首尾 `.` 与空格；**不截短**（与库内既有全标题目录一致；详见 `references/frontmatter_spec.md`）。

PDF 路径解析：
- `linkMode: 0`（imported）→ `~/Zotero/storage/<attachment-key>/<filename>.pdf`
- `linkMode: 1`（linked）→ 优先用 `get_item_details` 返回的 `attachments[].path` 绝对路径（若文件存在）

```bash
mkdir -p "<vault>/05 Literature/<paper_title>/"
mkdir -p "<vault>/05 Literature/<paper_title>/Figure_<paper_title>/"
cp "<resolved-pdf-path>" "<vault>/05 Literature/<paper_title>/"
```

`Figure_<paper_title>/` 是该篇高清 Figure PNG 的唯一输出目录（Step 6 渲染目的地），与论文目录同根、同名标题（同一套消毒规则，**不截短**）。

注意：Hermes 终端可接受 vault 外路径（无 Claude Code 的 Bash 拦截问题）。

### Step 4：MinerU 解析 PDF

**小 PDF（≲8MB）**：一键脚本即可。

```bash
python3 ~/.hermes/skills/research/paper-notes/scripts/mineru_upload.py \
  "<vault>/05 Literature/<paper_title>/<filename>.pdf" \
  "<vault>/05 Literature/<paper_title>/" \
  --language en
```

脚本执行：上传 → 轮询（最多 30 分钟）→ 下载 → 解压。输出 `<paper_title>/full.md`。

**大 PDF / 代理上传卡住**：不要反复空转 `mineru_upload.py`。按 `references/mineru_upload_proxy.md` 分步：
1. PyMuPDF 压到 ~120dpi JPEG PDF（可选但强烈推荐；压缩产物**仅供 MinerU OCR/结构解析**，最终插图一律从 Zotero 原 PDF 渲染——见 Step 6）
2. `file-urls/batch` 拿 URL + `batch_id`
3. `curl PUT --data-binary` 上传（无 Content-Type；走系统代理；长超时）
4. 轮询 `extract-results/batch/{id}`
5. `curl --noproxy '*'` 下 zip → 拷 `full.md` + `images/`

**Token 来源**（优先级）：
1. `$MINERU_TOKEN` 环境变量
2. 脚本内置默认值
3. 用户指定 `--token`

**下载失败处理**：CDN SSL 兼容性问题可能导致 `requests` 下载失败——脚本已内置 `curl` 回退。如果仍然失败，可用 `--download-only` 重试：
```bash
python3 ~/.hermes/skills/research/paper-notes/scripts/mineru_upload.py \
  "" "<vault>/05 Literature/<paper_title>/" \
  --download-only <batch_id>
```

解压后务必：`cp mineru_extract/full.md` 到笔记根，并把 `mineru_extract/images/` 拷到 `<paper_title>/images/`（供 md 相对路径引用）。MinerU 图片以普通相对 Markdown 链接 `![](images/<name>.jpg)` 呈现（不是 base64）——它们**只作定位线索**（识别 Figure 归属与页面），Step 5 清洗时全部删除，**不进入 `01 attachments`**，也绝不当最终插图（MinerU JPG 低像素，常 270×284）；最终插图由 Step 6 从 Zotero 原 PDF 渲染并落该篇 `<paper_dir>/Figure_<paper_title>/`。
### Step 5：清洗 MD（删除 MinerU JPG，不迁入附件）

```bash
python3 ~/.hermes/skills/research/paper-notes/scripts/clean_md.py \
  "<vault>/05 Literature/<paper_title>/full.md" \
  --in-place
```

不加 `--attachments-dir` 时保持旧行为：**删除** `![](images/<name>.jpg)` 图片引用（旧版 `--attachments-dir` 的「图片迁入附件目录」仅作为 CLI 兼容保留，**默认流程不再使用**）。

清洗内容：图片引用删除、`<details>`/`<table>` HTML 块、单字母面板标签、`(legend continued)` 行、`## Figure` → `**Figure ...**`。

**MinerU 原始 JPG 全部在此步删除**：清洗通过后删除论文目录 `images/`。MinerU JPG 低分辨率（常 270×284），只作定位线索，**不进入 `<vault>/01 attachments`**；高清插图由 Step 6 从 Zotero 原 PDF 渲染。

### Step 6：高清 Figure 渲染（Zotero 原 PDF → 300dpi PNG）🚨 图片唯一来源

所有最终插图**必须**来自 Zotero 原 PDF（`~/Zotero/storage/<attachment-key>/<filename>.pdf`，路径解析见 Step 3），用 `scripts/render_pdf_figure.py` 渲染 ≥300 dpi 无损 PNG；MinerU JPG 一律不作为最终插图。

对每张主图/补充图（顺序按 minerUmd 图注顺序）：

1. **定位 caption 页**：在 Zotero 原 PDF 中搜索该图图注文本（如 `Figure 1.` / `Fig. S1`）确定页码（1-based）
2. **渲染临时页面预览**：先用 `render_pdf_figure.py` 以整页 bbox 渲染到 `/tmp`（或 PyMuPDF 缩略图），供视觉确认
3. **视觉确认完整整图 bbox**：对照 caption 与正文，确认 bbox 恰好框住**完整整图**（含全部 panel 与子标签，不含 caption 文本、不含正文）
4. **渲染 300dpi PNG**：

```bash
python3 ~/.hermes/skills/research/paper-notes/scripts/render_pdf_figure.py \
  "$HOME/Zotero/storage/<attachment-key>/<filename>.pdf" \
  --page <caption 页码> --bbox <x0>,<y0>,<x1>,<y1> \
  --dpi 300 --output-dir "<vault>/05 Literature/<paper_title>/Figure_<paper_title>"
```

   输出文件名 = PNG 字节 SHA256（`<64位hex>.png`，同内容幂等复用）；stdout 为单行 JSON（`path`/`embed`/`width`/`height`/`page`/`bbox`/`dpi`）。**任何校验失败 → 非零退出且输出目录零写入**（`Figure_<paper_title>/` 未创建或未新增文件）。
5. **嵌入 minerUmd**：用 `patch`/`write_file` 把 `![[<64位hex>.png]]` 插到对应 Figure legend **之前**（同一 PNG 后续 Figure解读 直接复用同一 embed）

**每图只放一张完整整图**，禁止把 panel/图标碎片堆进笔记；某图无法从原 PDF 可靠定位完整整图时，该位置明确写「*原 PDF 未能可靠定位完整 Figure*」，**不得猜裁**。渲染失败（页码/bbox 越界、PDF 损坏）→ 检查原 PDF 路径与页码后重试，仍失败则写不可靠声明。

### Step 7：设置 frontmatter & 重命名

用 `read_file` 读取清洗后的 MD，用 `patch` 或 `write_file` 在文件开头添加 frontmatter（**不要**对中文论文frontmatter写非ASCII字符时省略）：

```yaml
---
title: minerUmd_<ckey>
citation key: <ckey>
zotero: zotero://select/library/items/<item_key>
date: YYYY-MM-DD
---
```

重命名文件：`full.md` → `minerUmd_<ckey>.md`

（详细规范见 `references/frontmatter_spec.md`）

### Step 8：清理临时文件

只删临时产物。Step 5 清洗成功（`clean_md.py` 非零退出即整体失败、MD 未动）、Step 6 高清渲染完成且 minerUmd 已嵌入 PNG 后，才允许清理论文目录 `images/`（如未删）与临时目录：

```bash
python3 -c "
import os, shutil
d = '<vault>/05 Literature/<paper_title>/'
for f in os.listdir(d):
    fp = os.path.join(d, f)
    if os.path.isfile(fp) and (f.endswith('.pdf') or f.endswith('.zip') or f in ('full.md','_copy.py')):
        os.remove(fp)
    if os.path.isdir(fp) and ('mineru_extract' in f or f == 'images'):
        shutil.rmtree(fp, ignore_errors=True)
"
```

图片已在 Step 5 全部删除（MinerU JPG 不保留）；高清 Figure PNG 由 Step 6 渲染并落入该篇 `<paper_dir>/Figure_<paper_title>/`（content-addressed `![[<64位hex>.png]]`，minerUmd 与 Figure解读 复用同一 embed）。**不删除** `Figure_<paper_title>/` 目录——其中的高清 PNG 是两篇笔记共用的最终插图。
### Step 9：创建 Figure解读笔记 🚨 质量关键

这是整个流程中**最需要人工质量把关**的步骤。**不要走捷径生成占位笔记**——每张 Figure 必须逐 panel 精读。

**也可独立触发**（目录里已有 `minerUmd_<ckey>.md`）：「Write complete Figure解读…from minerUmd」「补全 Figure解读」——跳过 Step 1–8，直接做本步（Step 6 的渲染步骤并入「独立任务」流程，见 `references/figure_interpretation.md`）。规范见 `references/figure_interpretation.md`（含「独立任务」与 SM 图注缺失处理）。

1. **读取** `<vault>/05 Literature/<paper_title>/minerUmd_<ckey>.md` 全文（含 Methods 里对 fig. S 的引用）
2. **通读正文**，理解每张 Figure 对应的叙事逻辑和实验方法
3. **图注补全**：minerUmd 图注残缺时用 Zotero 主 PDF（PyMuPDF）抽 Fig legend；Science 类主 PDF 常无 S1–Sn 全文图注——补充图按正文/Methods 逐图归纳并**声明非逐字图注**，禁止空图或伪造出版社图注
4. **按 `references/figure_interpretation.md` 格式**，为每张 Figure 撰写原文 legend + 四维解读
5. **图片嵌入（第一轮生成时）**：每张主图/补充图都在对应 `## Figure X/SX` 标题**之后、叙事之前**嵌入 Step 6 渲染的高清整图——`![[<64位hex>.png]]`（300dpi 无损 PNG，与 minerUmd 中**同一文件、同一 embed**，已由 `scripts/render_pdf_figure.py` 从 Zotero 原 PDF 渲染并落入该篇 `<paper_dir>/Figure_<paper_title>/`）。**每图只放一张完整整图**，禁止把 panel/图标碎片堆进笔记；无法从原 PDF 可靠定位完整 Figure 时明确写「*原 PDF 未能可靠定位完整 Figure*」，不得猜裁。
6. **Main figures**：逐 panel，原文 legend 在 `>` 引用块中，解读用 `- **是什么/为什么/怎么做/发现**`
7. **Supplementary figures**：**与主图相同格式**——逐图 `## Figure S…`＋图片嵌入＋`> **叙事位置**`＋逐 panel `**Panel X**` / `>` 原文 Legend / `- **是什么/为什么/怎么做/发现**`；S 图很多时可按主题分 `###` 块。缺出版社 legend（Science 类主 PDF 常无 S 图全文图注）时仍逐图排版，并在该图下标明「据正文/Methods 归纳，非出版社逐字图注」。
8. **「怎么做」增强**：湿实验须注明实验模型；干实验须注明数据来源（如 scVI/milopy/cell2location + 细胞数）；不确定就写不确定——详见 `references/figure_interpretation.md`
9. **「发现」字段**：核心结果/结论，不复读「是什么」——详见同文件
10. **不要删除原文 legend**，必须和解读合并在一起
11. **Frontmatter 须包含 `状态: 未读`**；**不要添加 `tags`、`type`**
12. **落盘**：`terminal` + Python `Path.write_text`；勿把中文 vault 路径设为 `terminal` 的 `workdir`

保存为：`<vault>/05 Literature/<paper_title>/Figure解读_<ckey>.md`

Frontmatter 格式（原 source 拆成两个**顶层**属性，禁止嵌套 map、禁止 list 式）：
```yaml
---
title: Figure解读_<ckey>
date: YYYY-MM-DD
citation key: <ckey>
minerU: "[[minerUmd_<ckey>]]"
zotero link: zotero://select/library/items/<item_key>
状态: 未读
---
```

**文首 `## Overview`**：frontmatter 之后、第一张 Figure 之前必须有 `## Overview` 节——4–6 条 bullet，每条以 `**维度名**：` 开头（中文），覆盖：研究背景/问题、研究脉络（问题→思路→分析主线）、主要方法、关键结论/意义。目标是读者看完 Overview 即理解全文脉络、背景与主要方法；不要写成段落。（详见 `references/figure_interpretation.md`）

### Step 10：最终确认

最终 `<paper_dir>/`（`<vault>/05 Literature/<paper_title>/`）目录下应有两篇笔记与一个 Figure 子目录：
- `minerUmd_<ckey>.md` — 清洗后的纯文本全文
- `Figure解读_<ckey>.md` — 原文 legend + 四维解读（是什么/为什么/怎么做/发现）
- `Figure_<paper_title>/` — 该篇全部高清 Figure PNG（content-addressed `![[<64位hex>.png]]`，两篇笔记复用同一 embed）

无残留 PDF、zip、临时目录（`Figure_<paper_title>/` 保留）。

---

## 任务路由（去重后 N 决定）

当用户要求导入 Zotero 合集或多个条目时，按以下策略执行。

### 去重计数规则

1. 用 `get_collection_items` 获取合集所有条目
2. 对比输出目录已有笔记，跳过已存在的
3. 用 `get_citekey.py --batch` 一次性获取所有 citation key
4. 按 Zotero **父条目**去重计数，记为 N

### 路由决策

- **N < 2**：当前主 Agent 完整执行 Step 1–10（单篇处理流程），不启动子 Agent
- **N ≥ 2**：自动为每篇启动一个独立 worker 并行执行，每个 worker 只处理一篇文献，禁止递归再分派

### MinerU 转换（N ≥ 2 时并行批处理）

对多篇 PDF，采用 **上传全部 → 统一轮询 → 统一下载**。

**推荐：同一 `file-urls/batch` 挂多文件（单 `batch_id`）**——比「每篇单独申请 batch」更省往返，轮询一次即可看齐全部状态。顺序：大 PDF 先压 ~120dpi（**仅供 MinerU OCR/结构解析**，插图从各篇原 PDF 渲染）→ POST 多文件 batch → 顺序 curl PUT（走代理）→ GET 统一轮询 → 各 zip `curl --noproxy '*'`。可复制步骤见 `references/mineru_multi_file_batch.md`；代理/压缩兜底见 `references/mineru_upload_proxy.md`。

```python
# 伪代码（多 batch 时仍可用）：
for paper in papers:
    batch_id = upload_pdf(pdf_path)
    batch_ids.append((paper, batch_id))

while pending:
    time.sleep(10)
    for bid in list(pending):
        state = check_status(bid)
        if state == "done":
            download_and_extract(bid)
            pending.remove(bid)
```

单 batch 多文件时等待时间接近最慢一篇。**下载失败**时用 `--download-only` 或按 zip URL 单独 curl。

下载完成后逐篇执行 **Step 5–8**（清洗 → 高清 Figure 渲染 → frontmatter → 清理）；每篇的最终 PNG 均落各自的 `<paper_dir>/Figure_<paper_title>/`（Step 6）。

### 并行生命周期与自动收尾（N ≥ 2）

主 Agent 始终是任务所有者。Hermesian/ACP 中只允许采用可等待、可获取退出状态的 worker 机制，不依赖用户消息触发收尾。

**推荐机制**：使用 `hermes chat -Q --source tool -s paper-notes -q "Write complete Figure解读... from minerUmd at <path>"` 启动独立进程，由主 Agent 通过受管进程的 wait/join 等待完成。

**生命周期规则**：
1. 输入：每个 worker 收到一篇的 minerUmd 绝对路径、figure_interpretation.md 引用、Frontmatter 字段、中文撰写指令、输出路径
2. 输出：每个 worker 直接写出该篇的 `minerUmd_<ckey>.md` 和 `Figure解读_<ckey>.md` 到对应目录，并记录状态
3. 主 Agent 等待全部 worker 进入成功/失败终态，再逐篇读回验证
4. 所有 worker 结束前不发送最终答复
5. 无 wait/join 能力时自动退化为顺序执行（主 Agent 逐篇处理 Step 1–9）
6. 个别文献失败不得阻塞已成功文献；最终一次性汇报成功、失败及原因

**Figure解读质量要求**（与单篇一致，不降级）：
- 每篇必须含 `**是什么**`、`**为什么**`、`**怎么做**`、`**发现**`
- 每张主图/补充图都须在 `## Figure X/SX` 标题后嵌入 Step 6 渲染的同一高清整图 `![[<64位hex>.png]]`（从 Zotero 原 PDF 300dpi 渲染、落该篇 `<paper_dir>/Figure_<paper_title>/`，多图按原顺序；不可靠时写「原 PDF 未能可靠定位完整 Figure」，不得猜裁）
- 主文体量通常 >10KB
- 遵循 `references/figure_interpretation.md` 完整规范

### 阶段 D：文献笔记总览 / 主题表填行（可选但常见）

导入后常写入 `05 Literature/🗺️文献笔记总览.md` 某主题表（如「单细胞milo分析」），列固定 **Title | Figure解读 | 总结**：

1. **Title**：完整英文标题（与同文件其它表一致，不用 citekey）
2. **Figure解读**：`[[Figure解读_<ckey>]]`（仅笔记名）
3. **总结**：2–3 条短 bullet，`<br>` 换行；**面向该主题的方法学要点**，非全文摘要
4. 用 `patch` 替换时把 `## 主题` + 表头 + 空行一并纳入唯一匹配块（编辑器选区限制时尤其重要）

**方法学主题表的 总结 铁律**：先在 minerUmd 检索方法名。真正跑了该 pipeline（Methods 写 milopy / KNN neighborhood DA）→ 写具体用法；**仅 References 引用、正文用 Wilcoxon/GLM 等** → 写清「引用 X；主文用 Y」，禁止写成「用 Milo 做了…」。

---


## 错误处理

| 场景 | 处理 |
|------|------|
| Zotero 搜索无结果 | 让用户确认标题是否正确 |
| PDF 附件不存在 | 告知用户该条目无 PDF 附件 |
| MinerU 上传失败（签名错误） | 检查 token 是否过期 |
| MinerU 上传卡死 / 前台超时 | PDF 过大或代理限速 → 按 `references/mineru_upload_proxy.md` 压缩 + curl 分步上传 |
| MinerU 轮询超时 | 告知用户，保留 batch_id 可稍后手动查询 |
| MinerU 下载 SSL 失败 | 脚本内置 curl 回退；仍失败用 `--download-only <batch_id>`；CDN 优先 `--noproxy '*'` |
| URL extract task 失败 | 改走本地 PDF 上传，勿依赖期刊直链 |
| Zotero SQLite 锁定 | `--batch` 模式自动处理（单次复制 DB 查询所有 key） |
| 批量 Figure解读 占位 | 🚨 必须走并行生命周期协议（N≥2 时 hermes chat -Q 或主 Agent 顺序执行），绝对不要 Agent 自己逐篇写 |
| Figure 渲染失败（原 PDF 打不开 / 页码或 bbox 越界 / dpi<300） | 检查 Zotero 原 PDF 路径、caption 页码与 bbox；`render_pdf_figure.py` 非零退出且输出目录零写入，修复后重试；仍失败则写「原 PDF 未能可靠定位完整 Figure」 |

## 注意事项

- PDF > 200MB 或 > 200 页 → MinerU 不支持，需用户手动拆分
- 非英文文献需传 `--language ch`（中文）等参数
- Figure 数量过多（>14 张主图）时告知用户并确认
- 批量导入时，已有笔记的论文自动跳过（检查 `minerUmd_*.md` 是否存在）
- Figure解读 将原 `source` 拆成两个**顶层**属性：`minerU: "[[minerUmd_<ckey>]]"`（minerUmd 笔记 wikilink）与 `zotero link: zotero://select/library/items/<item_key>`（zotero URI）；**禁止**嵌套在 `source:` 下（Obsidian 会显示成 JSON 对象），也禁止 list 式旧格式

## Gotchas（经验教训）

1. **Figure解读 是质量分水岭**——占位笔记 vs 完整解读是用户最能感知的质量差异。宁可慢，不降质。
2. **「发现」不是「是什么」的复读**——前者是结论（"亮氨酸最显著上调 HLA-DR"），后者是内容描述（"各氨基酸刺激下 HLA-DR⁺ 比例"）。如果混淆，整篇 Figure解读 会变成啰嗦的流水账。
3. **模型/来源不确定就写不确定**——编造的细胞系名或样本量比不写更糟糕。原文未说明模型时写「原文未明确说明」。
4. **MinerU CDN SSL + 本地代理冲突**——Python `requests` 对 `cdn-mineru.openxlab.org.cn` 常报 `SSLEOFError`。脚本内置 `curl` 回退。但若本地有 HTTPS 代理（如 `127.0.0.1:8080`），curl 也会因代理隧道的 TLS 握手失败（`SSL: UNEXPECTED_EOF_WHILE_READING`）。**解法**：`curl --noproxy cdn-mineru.openxlab.org.cn -L -o <zip> <url>` 或 `unset HTTP_PROXY HTTPS_PROXY` 后重试。也可用 `--noproxy '*'` 全局绕过代理下载。
5. **Zotero DB 并发查询**——快速连续调用 `get_citekey.py` 会导致临时文件覆盖和 `Cannot operate on a closed database`。用 `--batch` 一次性查询。
6. **write_file 对中文路径静默失败**——Hermes `write_file` / `execute_code` 在路径含中文（如 `知识库`）时可能报成功但不落盘。**必须用 `terminal` + Python `Path.write_text`**。另：**`terminal` 的 `workdir` 不能含中文**（会 Blocked: disallowed character）——省略 workdir 或用英文 cwd，绝对路径写在 Python 字符串内。
14. **独立 Figure解读 ≠ 文献卡片**——`literature-card-from-figure-notes` 只从已有 Figure解读拆卡片；**写完整 Figure解读**走本 skill Step 9 / `references/figure_interpretation.md`。
15. **SM 图注不在 minerUmd 里**——先扫 PDF 页数与是否含 `Fig. S`；仅有主文时用正文 `fig. Sx` + Methods 逐图归纳补充图 legend 并声明归纳来源，勿假装有完整 SM legend。
16. **Figure解读 提问须先查原文**——当用户针对 Figure解读 笔记提问（如"这个方法怎么做的""这个术语什么意思"），必须先查阅同目录下的 `minerUmd_*.md` 原文 Methods 部分，用原文的实际方法作答，而非依赖一般性推测或常见做法。教训：曾将 signature scoring 方法推测为 AUCell/H 矩阵，实为 Seurat AddModuleScore + 30-bin 对照基因方案（原文 Methods "Scoring gene sets" 明确写了公式）。先查原文再答，避免知识污染。
7. **OSS 上传在本地代理下极慢 / 易超时**——`mineru.oss-cn-shanghai.aliyuncs.com` 经 `http_proxy=127.0.0.1:8080` 时实测约 15–40 KB/s（18MB PDF 可卡死数分钟）。**优先**：PyMuPDF 压到 ~120dpi JPEG（**仅供 MinerU OCR，最终插图从原 PDF 渲染**）再 `curl PUT --data-binary`（无 Content-Type、走代理、长超时）；见 `references/mineru_upload_proxy.md`。不要对 OSS 用 `--noproxy '*'`（本环境曾卡在 Expect:100）。
8. **URL 提取 API 不可作期刊 PDF 主路径**——`POST /api/v4/extract/task` + `{"url":...}` 对 Cell/PMC/DSpace 等常 `failed to read file` 或拉到 HTML。优先本地 Zotero PDF。
9. **linkMode=1 仍可能有本地 path**——用 `attachments[].path` 若存在则直接 `cp`，勿只认 `~/Zotero/storage/<key>/`。
10. **高清 Figure 一律从 Zotero 原 PDF 渲染**——MinerU JPG（常 270×284，低像素）只作定位线索，Step 5 清洗时删除、不迁入 `01 attachments`；最终插图用 `scripts/render_pdf_figure.py` 以 300dpi 从原 PDF 裁完整整图（Step 6），content-addressed `![[<64位hex>.png]]` 落该篇 `<paper_dir>/Figure_<paper_title>/`，minerUmd 与 Figure解读 复用同一 embed。
11. **Figure legend OCR 残缺**——压缩 PDF 仅供 MinerU OCR，部分 panel 图注可能丢失；禁止空 panel 占位：用 RESULTS 正文补齐，并在引用块注明「（图注 OCR 不完整，据正文归纳）」。压缩不影响最终插图（插图从原 PDF 渲染）。
12. **批量 PUT 顺序优于并行**——同批多个 OSS URL 在本地 HTTPS 代理下并行上传易 thrash；顺序 PUT 更稳。
13. **主题总览表 ≠ 全文摘要**——填「milo/NMF/…」类表时，总结只写与该主题相关的分析设计；文献仅引用方法学原文时不要写成「使用了该方法」。

## 参考文件

- `references/figure_interpretation.md` — Figure 四维解读格式
- `references/frontmatter_spec.md` — frontmatter 规范
- `references/mineru_upload_proxy.md` — 大 PDF / 代理环境下的 MinerU 分步上传与下载
- `references/mineru_multi_file_batch.md` — 单 batch 多文件 file-urls 上传 + 统一轮询/下载
- `references/bulk_cdn_recovery.md` — 多篇论文 CDN 下载同时失败时的批量恢复脚本模板
- `scripts/render_pdf_figure.py` — 从 Zotero 原 PDF 渲染 300dpi 无损 PNG 整图（Step 6，输出落 `<paper_dir>/Figure_<paper_title>/`；测试见 `tests/test_hires_figure_pipeline.py`、`tests/test_figure_directory_policy.py`）

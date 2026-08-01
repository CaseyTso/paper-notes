# 笔记 Frontmatter 规范

## minerUmd 全文笔记

文件路径：`05 Literature/<paper_title>/minerUmd_<citation_key>.md`

```yaml
---
title: minerUmd_<citation_key>
citation key: <citation_key>
zotero: zotero://select/library/items/<item_key>
date: YYYY-MM-DD
---
```

- `title` 使用文件名（无 `.md` 后缀）
- `citation key` 从 Zotero SQLite 查询（`citationKey` 字段），用 `scripts/get_citekey.py` 获取
- `zotero` 使用 `zotero://select/library/items/<itemKey>` 格式
- `date` 使用当前日期
- **不要** 添加 `tags`、`type` 字段

## Figure解读 笔记

文件路径：`05 Literature/<paper_title>/Figure解读_<citation_key>.md`

```yaml
---
title: Figure解读_<citation_key>
date: YYYY-MM-DD
citation key: <citation_key>
minerU: "[[minerUmd_<citation_key>]]"
zotero link: zotero://select/library/items/<item_key>
状态: 未读
---
```

- `title` 使用文件名（无 `.md` 后缀）
- **原 `source` 字段已拆成两个顶层属性**（禁止再写 `source:` 嵌套 map 或 list）：
  - `minerU`：同目录 minerUmd 笔记的 wikilink，`[[minerUmd_<citation_key>]]`（无 `.md` 后缀）
  - `zotero link`：Zotero 条目 URI，`zotero://select/library/items/<itemKey>`
- ⚠️ 不要写成：
  ```yaml
  source:
    minerU: "..."
    zotero link: "..."
  ```
  Obsidian 会把嵌套 map 渲染成单个 JSON 对象属性，属性面板显示异常。
- `状态` 默认 `未读`，后续可改为 `已读`
- **不要** 添加 `tags`、`type` 字段
- **正文结构**：frontmatter 之后、第一张 Figure 之前**必须**有 `## Overview` 节（4–6 条 `**维度名**：` bullet 概述全文，规格见 `figure_interpretation.md`）
- **图片**：minerUmd 与 Figure解读 的高清 Figure 为**同一文件、同一 embed**——由 `scripts/render_pdf_figure.py` 从 Zotero 原 PDF 以 300dpi 渲染的 `![[<64位hex>.png]]`（content-addressed，落该篇 `<paper_dir>/Figure_<paper_title>/`），两篇笔记均直接引用，不复制、不改名；MinerU JPG 不进入附件目录

## paper_title 生成（文件夹名）

文件夹名使用文献**英文全标题**，消毒后作为目录名（**不截短**，与库内既有目录一致）：

1. 先剥 HTML 上标/下标标签：`<sup>+</sup>` → `+`、`<sub>…</sub>` → `…`
2. `/` → `-`
3. 去掉非法文件名字符：`<` `>` `:` `"` `|` `?` `*` `\`
4. 压缩连续空白为单个空格
5. 去掉首尾的 `.` 与空格

例如：`"P16+ Cells Drive Adverse Postischemic Cardiac Remodeling Through CCL8-Mediated Recruitment of Cytotoxic Lymphocytes"`（P16+ 来自 `<sup>+</sup>`）→ 目录名 `P16+ Cells Drive Adverse Postischemic Cardiac Remodeling Through CCL8-Mediated Recruitment of Cytotoxic Lymphocytes`。

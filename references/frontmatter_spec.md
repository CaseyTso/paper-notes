# Frontmatter 规范（canonical layout）

## 主条目：唯一权威书目记录

`05 Literature/<citation_key>/<citation_key>.md` 是该篇文献的**唯一权威**（authoritative）书目记录。MinerU 全文、Figure 解读、卡片、索引都不重复完整书目元数据；阅读状态只存在于主条目。

示例 frontmatter（v1）：

```yaml
---
schema_version: 1
paper_id: 550e8400-e29b-41d4-a716-446655440000
citation_key: shiauSpatiallyResolvedAnalysis2024
citation_key_aliases: []
item_type: article-journal

title: Spatially resolved analysis...
authors:
  - family: Shiau
    given: ...
journal: ...
journal_abbreviation: ...
publication_date: 2024-01-01
year: 2024
volume:
issue:
pages:
doi:
pmid:
pmcid:
arxiv:
url:
issn: []
language: en
abstract: ...

pdf_status: available
pdf_sha256: ...
reading_status: unread
archived: false
created_at: 2026-08-02T00:00:00+08:00
updated_at: 2026-08-02T00:00:00+08:00
metadata_sources: [crossref, pubmed]
field_provenance:
  title: crossref
  authors: pubmed
  abstract: pubmed
---
```

## Schema 规则

- `paper_id`：创建时生成的随机 UUID，永久机器身份；citation-key 重命名**不改变**它。
- `citation_key`：vault 全局唯一的人类可读身份，目录名与主文件名一致；旧 key 在重命名时追加进 `citation_key_aliases`（全局保留，不得分配给其他文献）。
- `publication_date` 接受已知精度：`YYYY` / `YYYY-MM` / `YYYY-MM-DD`；`year` 必须等于其年份分量（归一化检索字段）。
- `reading_status`：`unread` / `reading` / `read`。
- `pdf_status`：至少 `missing` / `available`；文件存在性由磁盘独立校验（`item reconcile` 提议修正）。
- 纯元数据条目合法（无 PDF）。
- 主类型：期刊论文（journal article）；兼容：预印本（preprint）；其他 Zotero 类型仅手动导入时做损失最小化 fallback。
- **禁止** volatile 指标字段：EasyScholar、IF、JCI、JCR、CAS partition 等一律**禁止**写入 schema（只在插件 UI 展示，见 SKILL.md）。
- **不持久化** Zotero item key 与 active `zotero://` 链接（迁移后不再写入）；PDF 身份 = canonical 路径 + 主条目 `pdf_sha256`。

## 目录语义（design §5）

```text
05 Literature/<citation_key>/
├── <citation_key>.md      ← 主条目（唯一权威，必选）
├── <citation_key>.pdf     ← canonical 主 PDF（被选定的主 PDF）
├── minerUmd_<citation_key>.md
├── Figure解读_<citation_key>.md
├── attachments/           ← 补充 PDF 与 supplementary 文件
├── cards/                 ← 仅本论文派生的卡片
└── figures/               ← 最终高清 Figure 资产目录
    └── <sha256>.png
```

- `figures/`：该篇**最终**高**清**（final high-resolution）Figure 资产目录；文件名保持内容哈希（`<sha256>.png`）；minerUmd 与 Figure解读 引用同一 embed（同一 PNG 文件、不复制不改名）。
- `cards/`：只放由**这一篇**论文**派生**（derived）的卡片；跨论文综合笔记留在全局位置。
- `attachments/`：**补充** PDF 与 supplementary 文件；主 PDF 之外的 PDF 一律进这里。
- 跨论文综合笔记留在全局位置（不进任何论文目录）。

## 派生笔记 frontmatter（最小关系字段）

MinerU 全文、Figure 解读、卡片只保留最小关系字段，不重复书目元数据：

```yaml
---
paper_id: 550e8400-e29b-41d4-a716-446655440000
citation_key: shiauSpatiallyResolvedAnalysis2024
paper: "[[shiauSpatiallyResolvedAnalysis2024]]"
---
```

- 阅读状态（`reading_status`）只存在于主条目，派生笔记不重复。
- **不要**添加 `tags`、`type` 字段。
- 命名：`minerUmd_<citation_key>.md`（清洗后的全文笔记）、`Figure解读_<citation_key>.md`。

## 迁移（legacy）对照

旧布局（legacy，已废弃）以论文英文全标题为目录名，frontmatter 含 `citation key` / `zotero` / `zotero link` / `状态` 字段，高清 PNG 落在 `<paper_dir>/Figure_<paper_title>/`；`migration` 会把这些条目转换为 canonical 布局：目录与主文件名改为 `<citation_key>`，移除 `zotero://` 依赖与重复状态字段（`状态` 映射为主条目 `reading_status`），figure 资产迁入 `figures/`。新流程不再使用旧字段、旧目录名与 `Figure_<paper_title>` 布局。详见 `migration.md`。

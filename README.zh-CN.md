# paper-notes

Obsidian-native 文献管理系统：vault 内的 Markdown/YAML 与文件是持久文献资产；`paper-notes` CLI 是唯一受管写入者，Obsidian 插件（只读 reader/索引/UI）通过版本化 JSON 协议调用它。**不要求 Zotero 运行时**——过渡期 Zotero 仅作只读来源，迁移会移除笔记中的 active `zotero://` 链接，迁移后不再写入。

[English](README.md)

## Canonical 目录布局

```text
05 Literature/<citation_key>/
├── <citation_key>.md      ← 主条目（唯一权威书目记录）
├── <citation_key>.pdf     ← canonical 主 PDF
├── minerUmd_<citation_key>.md
├── Figure解读_<citation_key>.md
├── attachments/           ← 补充 PDF 与 supplementary 文件
├── cards/                 ← 仅本论文派生的卡片
└── figures/               ← 最终高清 Figure 资产（<sha256>.png）
```

- 主条目 YAML 是权威（`schema_version` / `paper_id` / `citation_key` / `pdf_status` / `reading_status`）；派生笔记只保留最小关系字段。EasyScholar / IF / JCI / JCR / CAS partition 指标**禁止**写入 Markdown——它们是易变的 UI-only 数据，绝不写入任何笔记。
- 常规流程**不删除** canonical 主 PDF（`<citation_key>.pdf`）。
- 旧布局（论文全标题目录 + `<paper_dir>/Figure_<paper_title>/` 图子目录）已废弃；用 `migrate legacy-obsidian` 迁移（见 `references/migration.md`）。

## 工作流

- **条目管理**：`python3 -m paper_notes.cli item create|show|update|attach-pdf|reconcile|rename-key|delete`，`--json` envelope（`protocol_version` / `needs_confirmation`，退出码 0/2/3/4）。见 `references/cli_protocol.md`。
- **MinerU 转换与 Figure 解读仍是 Hermesian + paper-notes skill 工作流**（Obsidian 插件从不启动它们）：`scripts/mineru_upload.py --citation-key <key>` 定稿 `minerUmd_<citation_key>.md`；`scripts/clean_md.py` 把临时 MinerU 图片迁移进 `<paper_dir>/attachments/`；`scripts/render_pdf_figure.py` 从 canonical 主 PDF 渲染 ≥300dpi 完整整图到 `<paper_dir>/figures/`（内容哈希文件名，两篇笔记共享同一 embed）。Figure 解读质量规则（Overview、整图嵌入、source Methods、不猜 panel）见 `references/figure_interpretation.md`。
- **迁移**：`migrate legacy-obsidian --dry-run|--apply <run_id>`、`migrate verify <run_id>`、`migrate rollback <run_id>`；备份在 vault 外 `~/Library/Application Support/paper-notes/migrations/<run_id>/`，永不自动删除。

## 仓库布局

- `SKILL.md` — agent 工作流（Hermes skill）
- `references/` — 详细规范
- `scripts/` — 辅助命令行脚本
- `paper_notes/` — 核心 Python 包
- `tests/` — 回归测试

## 本地开发

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py paper_notes/*.py paper_notes/adapters/*.py paper_notes/migration/*.py
```

Python 3.11+（CI 中针对 3.11 与 3.13 测试）；`requests>=2.31,<3` 与 `PyMuPDF>=1.24,<2`（见 `requirements.txt`）。

## 实时联动警告

本仓库可能通过符号链接 `~/.hermes/skills/research/paper-notes` 作为 Hermes 当前使用的 skill。未提交的修改会立即影响新的 Hermes 会话。禁止提交 API Token、PDF、vault 内容或 Zotero 数据库。

## 许可证

[MIT](LICENSE)

# paper-notes

用于将 Zotero PDF 通过 MinerU 转换为 Obsidian 文献笔记，并生成详细 Figure 解读的 Hermes Agent skill。

[English](README.md)

## 目录

- `SKILL.md`：Agent 工作流
- `references/`：详细规范
- `scripts/`：辅助命令行脚本
- `tests/`：回归测试

## 前置条件

- Python 3.11+（CI 中针对 3.11 与 3.13 测试）
- `requests>=2.31,<3` 与 `PyMuPDF>=1.24,<2`（见 `requirements.txt`）
- 运行中的 Zotero（`get_citekey.py` 需要）、MinerU API token（`mineru_upload.py` 需要）、Obsidian vault（`clean_md.py` 与 `render_pdf_figure.py` 需要）
- `scripts/render_pdf_figure.py` 直接从 Zotero 原 PDF 渲染 ≥300dpi 无损 PNG 整图——笔记中最终插图的唯一来源（MinerU JPG 碎片只作定位线索，清洗时删除）。最终 PNG 统一写入每篇 `<paper_dir>/Figure_<paper_title>/`，minerUmd 与 Figure解读 引用同一 `![[<64hex>.png]]`。

## 本地开发

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

## 实时联动警告

本仓库可能通过符号链接 `~/.hermes/skills/research/paper-notes` 作为 Hermes 当前使用的 skill。未提交的修改会立即影响新的 Hermes 会话。禁止提交 API Token、PDF、vault 内容或 Zotero 数据库。

## 许可证

[MIT](LICENSE)

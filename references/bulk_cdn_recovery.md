# 批量 MinerU CDN 下载恢复

当 `mineru_upload.py` 上传成功但下载失败（`cdn-mineru.openxlab.org.cn` 代理/SSL），多篇论文时单手修复效率低。本文件给批量恢复模板。

## 触发条件

- 多篇论文 `mineru_upload.py` 均报 `RuntimeError: Failed to download ... via both requests and curl`
- 输出目录已有 `mineru.zip`（可能为 0 字节或残骸）和 `mineru_extract/`
- 需要从错误信息中提取各论文的 `zip_url` 统一下载

## 批量恢复脚本模板

```python
import subprocess, zipfile, shutil
from pathlib import Path

vault = Path("/path/to/vault/05 Literature")

# 键 = canonical citation key 目录名（见 frontmatter_spec 的 canonical 布局）
papers = {
    "citation_key_1": {
        "zip_url": "https://cdn-mineru.openxlab.org.cn/pdf/.../xxx.zip",
    },
    "citation_key_2": { ... },
}

for key, info in papers.items():
    d = vault / key
    zip_path = d / "mineru.zip"
    extract_dir = d / "mineru_extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run([
        "curl", "--noproxy", "*", "-L", "-o", str(zip_path),
        info["zip_url"]
    ], timeout=300)

    if zip_path.stat().st_size > 1000:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        shutil.copy2(extract_dir / "full.md", d / "full.md")
        if (extract_dir / "images").exists():
            shutil.rmtree(d / "images", ignore_errors=True)
            shutil.copytree(extract_dir / "images", d / "images")
```

## 步骤

1. 从各进程输出中提取 `zip_url`（`Failed to download https://cdn-mineru...zip`）
2. 填入脚本 `papers` dict（键 = `05 Literature/<citation_key>/` 目录名）
3. 运行 → 全部并行下载
4. 验证每目录有 `full.md`（>10KB）和 `images/`（>0 文件）

## 后续

下载完成后继续 `clean_md.py`（`--attachments-dir <paper_dir>/attachments/` 迁移 MinerU 图片为该篇附件 embeds；MinerU JPG 只作定位线索，绝不进 `figures/`）→ `render_pdf_figure.py` 从各篇 canonical 主 PDF（`<paper_dir>/<citation_key>.pdf`）渲染 300dpi 高清整图入各篇 `<paper_dir>/figures/` → frontmatter → 定稿为 `minerUmd_<citation_key>.md`，然后并行写 Figure解读（与 minerUmd 复用同一 `![[<64位hex>.png]]` embed）。

> 旧布局（legacy）曾以论文全标题为目录名、把高清 PNG 落 `<paper_dir>/Figure_<paper_title>/`；该布局已废弃，迁移后统一使用 `<citation_key>` 目录与 `<paper_dir>/figures/`（详见 `migration.md`）。

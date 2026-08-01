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

# 键 = 消毒后的论文全标题目录名（见 frontmatter_spec 的 paper_title）
papers = {
    "paper_title_1": {
        "zip_url": "https://cdn-mineru.openxlab.org.cn/pdf/.../xxx.zip",
    },
    "paper_title_2": { ... },
}

for name, info in papers.items():
    d = vault / info["paper_title"]
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
2. 填入脚本 `papers` dict
3. 运行 → 全部并行下载
4. 验证每目录有 `full.md`（>10KB）和 `images/`（>0 文件）

## 后续

下载完成后继续 `clean_md.py`（不带 `--attachments-dir`；MinerU JPG 只作定位线索，清洗时删除引用与 `images/`，不迁入附件）→ `render_pdf_figure.py` 从各篇 **Zotero 原 PDF** 渲染 300dpi 高清整图入各篇 `<paper_dir>/Figure_<paper_title>/`（`<paper_dir>` = `<vault>/05 Literature/<paper_title>/`）→ frontmatter → 重命名为 `minerUmd_<ckey>.md`，然后 `delegate_task` 并行写 Figure解读（与 minerUmd 复用同一 `![[<64位hex>.png]]` embed）。

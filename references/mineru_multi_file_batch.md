# MinerU：单 batch 多文件上传（批量导入实测）

适用于一次摄入 2–N 篇本地 PDF（canonical 布局下，PDF 位于各篇 `05 Literature/<citation_key>/<citation_key>.pdf`）。

## 何时用

- 批量导入；希望 **一个 `batch_id` 统一轮询**
- 大 PDF 已压到约 5–8MB（见 `mineru_upload_proxy.md`；压缩仅供 MinerU OCR，插图从各篇 canonical 主 PDF 渲染）

## 流水线

### 1. 申请 URL

```python
import os, requests, json
token = os.environ["MINERU_TOKEN"]
files = [{"name": "paper_a.pdf"}, {"name": "paper_b.pdf"}, {"name": "paper_c.pdf"}]
r = requests.post(
    "https://mineru.net/api/v4/file-urls/batch",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={"files": files, "model_version": "vlm", "language": "en"},
    timeout=60,
)
meta = r.json()
batch_id = meta["data"]["batch_id"]
urls = meta["data"]["file_urls"]  # 与 files 同序
```

### 2. 顺序 PUT（走代理）

```bash
# 对每个 (local_pdf, url)：
curl -X PUT --data-binary @"$PDF" -H "Content-Type:" \
  --max-time 900 -x http://127.0.0.1:8080 \
  -o /tmp/oss_resp.txt -w "http=%{http_code} size=%{size_upload}\n" "$URL"
# 期望 http=200
```

- **顺序上传** 比并行 PUT 更稳（本机代理易 thrash）
- **不要** 对 OSS 使用 `--noproxy '*'`

### 3. 轮询

```python
# GET https://mineru.net/api/v4/extract-results/batch/{batch_id}
# data.extract_result[]: file_name, state (waiting|running|done|failed), full_zip_url
# 全部 done/failed 后落盘 meta
```

实测：小 PDF 常在上传后很快 `done`；稍大一篇可能多轮 `running`（15s 间隔轮询即可）。

### 4. 下载与落盘

```bash
curl --noproxy '*' -L -o "$OUT/mineru_result.zip" --max-time 180 "$ZIP_URL"
unzip -o "$OUT/mineru_result.zip" -d "$OUT/mineru_extract"
# 各篇 output_dir = 05 Literature/<citation_key>/；清洗后的笔记定稿为 minerUmd_<citation_key>.md
```

然后按主 skill 逐篇：**clean_md（`--attachments-dir <paper_dir>/attachments/` 把 MinerU 图片迁移为该篇附件 embeds；MinerU JPG 只作定位线索，绝不进该篇 `figures/`）→ render_pdf_figure.py（从各篇 canonical 主 PDF `<paper_dir>/<citation_key>.pdf` 渲染 300dpi 高清整图入各篇 `<paper_dir>/figures/`）→ frontmatter（派生笔记最小关系字段）→ 删除临时产物（zip / mineru_extract / images 暂存；**不删除** `<citation_key>.pdf` 与 `figures/` 内容）。** clean_md 非零退出即整体失败、MD 未动。

> 旧布局（legacy）曾以论文全标题为目录名、把高清 PNG 落 `<paper_dir>/Figure_<paper_title>/`；该布局已废弃，迁移后统一使用 `<citation_key>` 目录与 `<paper_dir>/figures/`（详见 `migration.md`）。

## 与单篇脚本关系

- `scripts/mineru_upload.py`：单篇一键；小 PDF 优先（带 `--citation-key <ckey>` 定稿 `minerUmd_<citation_key>.md`）
- 本页：多篇同批 / 大 PDF 压缩后批量
- CDN/SSL/代理坑：仍以 `mineru_upload_proxy.md` 为准

## 本会话规模参考（2026-07）

| 原 PDF | 页数 | 策略 | 上传体积 |
|--------|------|------|----------|
| ~4.6MB | 17 | 原件 | ~4.6MB |
| ~22MB | 40 | 120dpi JPEG | ~6.4MB |
| ~32MB | 33 | 120dpi JPEG | ~5.8MB |

同批 `batch_id` 一次轮询即可全部 `done`。

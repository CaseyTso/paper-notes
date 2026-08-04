# MinerU 上传 / 代理速查（本机实测）

## 症状

- `python mineru_upload.py ...` 停在 `Uploading to MinerU:` 后 Hermes 前台 600s 超时
- curl PUT 到 OSS 进度长期停在 17% 或 `speed_upload` < 40k
- CDN 下载 zip 报 SSL / 代理隧道 EOF

## 环境特征

| 变量 | 典型值 |
|------|--------|
| `http(s)_proxy` | `http://127.0.0.1:8080` |
| API `mineru.net` | 经代理正常（秒级） |
| OSS 上传 | 经代理极慢但可完成 |
| `cdn-mineru.openxlab.org.cn` 下载 | **优先 `--noproxy '*'`** |

## 推荐手动流水线（大 PDF > ~8MB）

canonical 布局下，输出目录 = vault 内 `05 Literature/<citation_key>/`（主 PDF 已在该目录，即 `<citation_key>.pdf`）。

```bash
source ~/.zshrc   # MINERU_TOKEN
PDF_ORIG="<paper_dir>/<citation_key>.pdf"
OUT="<paper_dir>"   # = <vault>/05 Literature/<citation_key>
mkdir -p "$OUT"

# 1) 可选：压到 ~6MB 量级（PyMuPDF；压缩产物仅供 MinerU OCR/结构解析，最终插图从 canonical 主 PDF 渲染）
python3 - <<'PY'
import fitz, os
src, dst = "PDF_ORIG", "OUT/paper_small.pdf"  # 替换路径
doc = fitz.open(src)
out = fitz.open()
mat = fitz.Matrix(120/72, 120/72)
for i in range(doc.page_count):
    pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
    img = pix.tobytes("jpeg", jpg_quality=55)
    w, h = pix.width * 72/120, pix.height * 72/120
    p = out.new_page(width=w, height=h)
    p.insert_image(p.rect, stream=img)
out.save(dst, garbage=4, deflate=True)
print(os.path.getsize(dst))
PY

# 2) 申请上传 URL
python3 - <<'PY'
import os, requests, json
token=os.environ["MINERU_TOKEN"]
r=requests.post("https://mineru.net/api/v4/file-urls/batch",
  headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
  json={"files":[{"name":"paper.pdf"}],"model_version":"vlm","language":"en"}, timeout=30)
open("/tmp/mineru_meta.json","w").write(r.text)
print(r.json()["data"]["batch_id"])
print(r.json()["data"]["file_urls"][0][:100])
PY

# 3) curl 上传（无 Content-Type；走代理；长超时）
URL=$(python3 -c "import json;print(json.load(open('/tmp/mineru_meta.json'))['data']['file_urls'][0])")
curl -X PUT --data-binary @"$OUT/paper_small.pdf" -H "Content-Type:" \
  --max-time 900 -x http://127.0.0.1:8080 -o /tmp/oss_resp.txt -w "http=%{http_code} size=%{size_upload}\n" "$URL"
# 期望 http=200

# 4) 轮询
BATCH=$(python3 -c "import json;print(json.load(open('/tmp/mineru_meta.json'))['data']['batch_id'])")
# GET https://mineru.net/api/v4/extract-results/batch/$BATCH → state=done → full_zip_url

# 5) 下载 zip（绕过代理）
curl --noproxy '*' -L -o "$OUT/mineru_result.zip" --max-time 180 "$ZIP_URL"
unzip -o "$OUT/mineru_result.zip" -d "$OUT/mineru_extract"
cp "$OUT/mineru_extract/full.md" "$OUT/full.md"
mkdir -p "$OUT/images" && cp -R "$OUT/mineru_extract/images/"* "$OUT/images/" 2>/dev/null || true
# 图片暂存论文目录 images/ 仅作定位线索（MinerU JPG 低像素，绝不作为最终插图）；
# 清洗时 clean_md.py 把它们迁移到 <paper_dir>/attachments/（或按旧调用不带 --attachments-dir 直接删除引用）；
# 高清插图由 render_pdf_figure.py 从 canonical 主 PDF 渲染（主 SKILL「高清 Figure 渲染」节）
```

## 不要做

- 不要把 Cell/PMC 付费墙 HTML URL 丢给 `/api/v4/extract/task` 当主路径
- 不要对 OSS PUT 使用 `--noproxy '*'`（本环境曾导致 Expect:100 后长时间无进度）
- 不要在 CDN 下载时强制走 7897 代理（优先 noproxy）
- 不要把 MinerU 输出目录指向该篇 `figures/`（最终 figure 资产只由 `render_pdf_figure.py` 写入）

## 与脚本关系

`scripts/mineru_upload.py` 适合小 PDF 一键跑通；带 `--citation-key <ckey>` 时清洗后的笔记直接定稿为 `minerUmd_<citation_key>.md`（不带则保持旧 `full.md` 名）。大 PDF / 代理卡顿时改用本页分步流水线，再用 `clean_md.py`（`--attachments-dir <paper_dir>/attachments/` 迁移 MinerU 图片为 Obsidian embeds，或按旧调用删除图片引用）→ `render_pdf_figure.py`（从 canonical 主 PDF 渲染高清 Figure 到 `<paper_dir>/figures/`）→ 后续 frontmatter 步骤。

# 迁移设计（legacy Obsidian → canonical layout）

迁移把旧「论文全标题目录」布局逐步转换为 canonical citation-key 布局（`05 Literature/<citation_key>/`）。Zotero 在过渡期**保持只读**：迁移**绝不修改或删除** Zotero 数据库/存储，也不自动迁移整个 Zotero 库——只迁移用户明确选定的文献集合（当前为库内既有 Obsidian 文献目录 + 用户后续选定的条目）。

## run_id 生命周期

每次迁移有一个唯一 `run_id`。外部事务状态（备份、manifest、journal）全部位于 vault/仓库之外：

```text
~/Library/Application Support/paper-notes/migrations/<run_id>/
```

备份**永不自动删除**，供事后 verify / rollback / 审计。

## 命令

```text
paper-notes migrate legacy-obsidian --dry-run
paper-notes migrate legacy-obsidian --apply <run_id>
paper-notes migrate verify <run_id>
paper-notes migrate rollback <run_id>
```

### dry-run（只读）

- 读取前先把 Zotero SQLite 复制为临时快照（只读访问，锁冲突由单次快照规避）。
- 逐目录分类：已是 canonical（`<key>/<key>.md`）vs legacy（旧 frontmatter 字段 `citation key` / `zotero` / `zotero link` / `状态`，旧目录名 = 论文全标题）。
- 匹配既有 citation key、笔记、PDF、MinerU、Figure 解读、figure 资产、卡片；检测 path/key/alias/UUID 冲突。
- 列出每一项 move / copy / rename / edit；计算源 hash、数量、体积；检查可用磁盘空间与 backlink。
- 主 PDF 有歧义时**要求显式选择**（primary-PDF confirmation）。
- 全程零写入 vault；manifest 只落 state root。

### apply

1. 先建外部备份（上述 `migrations/<run_id>/`）。
2. 在**同一文件系统**的 staging 中构建完整新条目；校验 schema、hash、链接、生成索引。
3. 校验通过后才切换到最终路径；**绝不覆盖不同内容的目标**。
4. 失败自动恢复原路径；也支持按 `run_id` 手动 rollback。
5. 重复运行同一迁移幂等；非一致变更停止交人工复核（confirmation 流）。

### verify

对照 `<run_id>` 备份校验 applied 状态：目标内容、schema、hash、链接与索引是否与迁移产物一致；不一致给出结构化诊断（不修改任何内容）。

### rollback

按备份恢复原目录树。同样经过期望态校验：只有当前状态 == 该 run 的受管产物时才恢复/删除对应目标；期间的外部人工修改保留并报 conflict，绝不覆盖。

## 迁移内容

- 目录：`05 Literature/<论文全标题>/` → `05 Literature/<citation_key>/`。
- 主条目：legacy frontmatter → canonical `<citation_key>.md`（`schema_version` / `paper_id` / `citation_key` / `pdf_status` / `reading_status` …）。
- **移除**旧 `zotero://` 依赖与过时重复状态字段（legacy `状态` 映射为主条目 `reading_status`；派生笔记不再重复书目元数据与状态）。
- PDF：选定主 PDF 落 `<citation_key>.pdf`（SHA-256 记录在主条目）；其余 PDF / 补充文件进 `attachments/`。
- 派生笔记：`minerUmd_<citation_key>.md`、`Figure解读_<citation_key>.md` 保留内容，frontmatter 收敛为最小关系字段（`paper_id` / `citation_key` / `paper: "[[<citation_key>]]"`）。
- 卡片进 `cards/`；旧高清图资产迁移到 `figures/`（旧布局 `<paper_dir>/Figure_<paper_title>/` 不再使用）。

## 退役条件（Zotero 手动退役前必须全部满足）

1. 选定的既有文献全部通过 migration verification。
2. 新条目可不经 Zotero 创建。
3. 引用 picker 不再查询 Zotero/BBT。
4. DOCX/PDF 导出不再使用 `zotero.lua`。
5. EasyScholar 不再依赖 Zotero 配置。
6. 用户完成人工内容与 UI 验收。
7. 外部迁移备份保持可用。

# CLI 协议规范（JSON envelope）

`paper-notes` 是 Obsidian-native 文献系统的**唯一受管写入者**。Obsidian 插件与 Hermes agent 对文献的全部受管变更（建条目、更新、附加 PDF、重命名 key、删除、迁移、索引重建）都通过 CLI 完成；插件只直接读 Markdown 做响应式索引，绝不实现第二套 citation-key / 去重 / 迁移 / YAML 变更引擎。

## 命令面

```text
paper-notes item create|show|update|attach-pdf|reconcile|rename-key|delete
paper-notes card create
paper-notes moc create
paper-notes index rebuild|validate-manuscript
paper-notes metrics query
paper-notes config easyscholar
paper-notes migrate legacy-obsidian|verify|rollback
paper-notes version
```

- `item create`：接受 `--doi` / `--pmid` / `--pmcid` / `--arxiv` / `--url` / `--pdf`（可重复），需要 `--vault`；从结构化来源（PubMed/Crossref/arXiv 等）取元数据，冲突或关键字段缺失时返回 `needs_confirmation` 候选值；`--confirmed <json>` 提供用户确认值。
- `item attach-pdf`：主 PDF 复制进 `<paper_dir>/`（SHA-256 校验，源文件不移动不删除）；补充文件进 `attachments/`。
- `item rename-key` / `item delete`：先 dry-run 影响清单，再确认执行；重命名是 parser-aware 的事务性全局重命名，旧 key 追加到 `citation_key_aliases`。
- `card create`：从 Figure解读 选区派生卡片，落 `<paper_dir>/cards/`；需要 `--vault` / `--key` / `--title` / `--selection-file`；可选 `--filename`（覆盖 `card_<slug>.md` 默认名）、`--anchor-name` + `--source-note`（在源笔记选区末尾幂等插入 `^anchor` 并生成回链）、`--backlink`（在源笔记 anchor 后插入 `> 卡片：[[<card>]]` 构成显式双链）。目标已存在 → `conflict`；选区无法定位 → `card_warning`（卡片仍创建）；回链已存在 → `card_warning`（幂等）。
- `moc create`：在 `05 Literature/MOCs/` 下创建 Topic MOC 笔记（`kind: topic-moc` + 空四列表格）；需要 `--vault` / `--title`（即文件名，CJK 保留）；目标已存在 → `conflict`；空标题或含路径分隔符 → `error`。
- `index rebuild`：确定性重建 `.paper-notes/library.json` 与 `citation-aliases.json`；生成文件不得手改。
- `migrate legacy-obsidian`：只读发现 + 迁移计划（详见 `migration.md`）。

## JSON envelope（`--json`）

所有命令支持 `--json`，stdout 输出**恰好一个**版本化 envelope；人类可读诊断只走 stderr：

```json
{
  "protocol_version": 1,
  "status": "success | needs_confirmation | conflict | error",
  "data": {},
  "warnings": [],
  "errors": []
}
```

- `protocol_version`：恒为 `1`（当前版本字面量）。
- `status`：`success` 完成；`needs_confirmation` 需要用户确认候选值（含冲突解决候选）；`conflict` 与期望状态不一致、操作零写入；`error` 用户/配置/校验错误。
- `warnings` / `errors`：`Issue` 对象数组，每项 `{code, message, path?, field?}`——`code` 与 `message` 是稳定的机器/人类配对；`path`/`field` 定位问题。异常 repr（可能携带密钥）绝不进入 message。

## 退出码

| 码 | 含义 |
|----|------|
| 0  | success / needs_confirmation |
| 2  | 用户/配置/校验错误（error） |
| 3  | conflict |
| 4  | 内部/IO 错误（CLI 边界为未预期异常兜底） |

## needs_confirmation 确认流

1. `item create` 遇冲突或缺失关键字段 → 返回 `needs_confirmation`，`data` 携带候选值与来源（`candidates` / `conflicts`）。
2. 用户确认后，把确认值写成 JSON 文件，`--confirmed <file>` 重跑。
3. 用户确认值优先于远程来源；每个写入字段保留来源溯源（`metadata_sources` / `field_provenance`）供冲突复核。
4. citation-key 分配与 paper 身份（`paper_id`）**永不由 AI 建议**，只由 CLI 确定性分配。

## 不变式

- 写前校验 schema；短时工作区写锁；变更先 stage、支持原子替换；成功后重建索引；失败不留半成品条目。
- 插件在别的受管操作持锁期间禁止写操作；陈旧锁移除需确认。
- 任何写入路径不落 active `zotero://` 链接（migration 后不再使用）、Zotero item key、EasyScholar/IF/JCI/JCR/CAS 指标字段（详见 `frontmatter_spec.md`）。

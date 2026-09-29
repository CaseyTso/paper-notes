# CLI 协议规范（JSON envelope）

`paper-notes` 是 Obsidian-native 文献系统的**唯一受管写入者**。Obsidian 插件与 Hermes agent 对文献的全部受管变更（建条目、更新、附加 PDF、重命名 key、删除、迁移、索引重建、MOC 创建）都通过 CLI 完成；插件只直接读 Markdown 做响应式索引，绝不实现第二套 citation-key / 去重 / 迁移 / YAML 变更引擎。

## Managed/cooperative writer 契约（Owner 决策 A）

一致性保证的边界是**协作 writer 契约**，不是内核级多文件事务：

- **唯一锁域**：全部受管 writer（item/card/moc/mineru/index rebuild/migration 等）共用同一个 `<vault>/.paper-notes/write.lock`。锁 metadata 中的 `operation` 字段（`create_card`、`create_moc`、`migrate` 等）只是记账信息，**不是**独立锁域——任何持锁操作都会与其他所有受管操作互斥。
- **advisory lock**：该锁是协作约定，**不阻止**不检查锁的外部脚本、外部编辑器、已打开 writable `MAP_SHARED` mmap 或旁路代码直接写入 vault。它们在一致性保证范围之外（普通 path swap、symlink、常见 write 仍尽量检测，作为纵深防御，但不是完整 CAS）。
- **atomic 的精确含义**：atomic 只指**单 pathname** 的 rename/replace publish（`O_EXCL`/`os.replace`/`RENAME_EXCL`）；source + card 是 P0–P3 协作事务（受锁与插件 freeze 约束），**不是**内核多文件原子事务。
- **插件/调用者职责（exact card create 前）**：① 保存 source editor 缓冲区并**等待磁盘写入完成**（save-await）；② 基于已保存的 bytes **重新计算** UTF-8 byte offset 与选区字节，不得复用编辑器内部 offset；③ CLI 生命周期内**冻结**该 source 的编辑、重复 card-create 与一切会触发保存的交互；④ 不直接插入 anchor 或修改 card，业务 mutation 全部交 CLI；⑤ CLI 结束后**先重新加载** source/card 再解除冻结；⑥ lock conflict、取消、异常路径均在 `finally` 中刷新并解冻。若只能禁用按钮而无法阻止 source buffer 在 CLI 期间自动保存，则未满足本契约。
- **partial semantics（失败语义分级）**：① **success-with-warning**：exact 选区陈旧/越界/跨块仍成功创建卡片（rc 0，无死链），仅 source byte-for-byte 零写入——这不叫“语义失败零操作写”，卡片写入本身已发生；② **pre-mutation 校验/锁冲突**：各命令明示时零写入（含 migration 锁冲突：零 vault 写、零 journal 写）；③ **mutation 后检测到的 conflict/recovery**：如 migration apply 中途发现 source 缺失/目标差异，可能已写 journal 并执行自动恢复（CLI 仍返回 conflict/rc 3，但并非全程零写）；④ **运行失败**（P2/P3 之间 crash、identity mismatch 拒绝覆盖、release 失败）可能留下明确的 partial 状态或 residue，由结构化错误/residue 报告指明，不再宽泛承诺所有失败都零半成品。清理以 identity-safe 方式进行，不会删除非本事务拥有的文件。

## 命令面

```text
paper-notes item create|show|update|attach-pdf|reconcile|rename-key|delete
paper-notes card create
paper-notes moc create
paper-notes mineru convert
paper-notes index rebuild|validate-manuscript
paper-notes metrics query
paper-notes config easyscholar
paper-notes config mineru status|set-key --stdin|delete-key
paper-notes migrate legacy-obsidian|verify|rollback
paper-notes version
```

- `item create`：接受 `--doi` / `--pmid` / `--pmcid` / `--arxiv` / `--url` / `--pdf`（可重复），需要 `--vault`；从结构化来源（PubMed/Crossref/arXiv 等）取元数据，冲突或关键字段缺失时返回 `needs_confirmation` 候选值；`--confirmed <json>` 提供用户确认值。
- `item create --web-capture <json>`：接受 Browser Connector V1 Web Capture（schema v1，严格字段白名单）。网页证据永不视为 `confirmed`；官方来源优先，冲突/缺关键字段/疑似重复进入 `needs_confirmation`。复审提交使用 `--confirmed <json>` + `--confirm-token <token>`；token 绑定 capture 载荷、动作与目标指纹，陈旧/重放/不匹配 → `conflict`（rc 3，零写入）。`--web-capture` 不得与 `--doi/--pmid/--pmcid/--arxiv/--url/--pdf` 组合。
- `item attach-pdf`：主 PDF 复制进 `<paper_dir>/`（SHA-256 校验，源文件不移动不删除）；补充文件进 `attachments/`。
- `item rename-key` / `item delete`：先 dry-run 影响清单，再确认执行；重命名是 parser-aware 的事务性全局重命名，旧 key 追加到 `citation_key_aliases`。
- `card create`：从 Figure解读 选区派生文献卡片，落 `<paper_dir>/cards/`；需要 `--vault` / `--key` / `--title` / `--selection-file`；可选 `--filename`（覆盖 `card_<slug>.md` 默认名；必须为单一安全相对文件名，禁止绝对路径、正反斜杠、单点/双点、NUL 字节及目录穿越，非单一安全文件名拒绝为 `error`（rc 2）且零写入；保留中文文件名并自动追加 `.md`）。
  - **Exact byte-range 模式（推荐/插件默认）**：提供成对 `--source-start-byte <int>` 与 `--source-end-byte <int>`（0-indexed UTF-8 byte 半开区间 `[start, end)`）及 `--source-note`（必须为规范笔记名 `Figure解读_<key>` 或 `Figure解读_<key>.md`）。CLI 唯一定位选区所属 Markdown 顶层 block 并确定性插入 16-hex ASCII anchor（`^card-<hash>`），卡片生成 `> 参见 [[Figure解读_<key>#^<anchor>|<source_note>]]` 单向回链。exact 模式不写源笔记 visible backlink；若传 `--backlink` 或 `--anchor-name` 明确报 `error`（rc 2）。
  - **Legacy 模式**：不传 byte offsets 时保持既有兼容：仅传基本参数创建无回链卡片；或显式传 `--anchor-name` + `--source-note`（模糊匹配）及可选 `--backlink`（在源笔记 anchor 后插入 `> 卡片：[[<card>]]` 显式双链）。旧模式 warning code 保持 `card_warning`。
  - **返回值与路径**：`data.path` 恒为 vault-relative POSIX 路径（如 `05 Literature/<key>/cards/card_<slug>.md`，绝非绝对路径）。JSON 数据新增字段 `data.stem`（卡片文件名 stem）与 `data.anchor_status`（`"inserted" | "existing" | "failed" | null`）。保留现有字段 `citation_key`、`paper_id`、`anchor_name`、`anchor_inserted`、`anchor_link`、`backlink_inserted`。
  - **Anchor 容错与稳定 warning**：exact 模式下若选区范围越界、文本陈旧不匹配、跨多顶层块（含跨 Setext heading 与后续内容）或仅选 Setext heading（保守 fail closed），卡片仍成功创建（Envelope `status: "success"`，退出码 0），`anchor_link: null`，卡片内无死链接；源笔记保持 byte-for-byte 零写入；同时产生稳定 warning（`code: "card_anchor_failed"`，`path` 为规范相对路径 `05 Literature/<key>/Figure解读_<key>.md`），警告信息绝不泄露选区正文。
  - **Selection 文件读取**：按 raw bytes 读取并严格 UTF-8 decode，禁止 universal-newline 归一化 CRLF，与源文件逐字节对齐。解码错误进入 `error`（rc 2），不泄露文件内容。
  - **Symlink 边界安全（分级）**：exact card/source 写路径与 lock 实现（cards、source note、``.paper-notes/write.lock``）从已解析的 vault 根通过目录 fd 配合 ``O_DIRECTORY | O_NOFOLLOW`` 逐级验证（vault 内相关 symlink fail closed，根本身是 symlink 时以解析受信锚正常工作）。MOC create、index rebuild、migration 等其余受管命令按 pathname 写入，不承诺统一 dirfd/O_NOFOLLOW：vault 内预置 symlink 会被跟随，其安全保证依赖 cooperative lock 与各命令自身的冲突/目标检查（目标已存在、零写冲突等）；不在 exact card 事务路径上的写入请勿以 nofollow 边界为由宣称防 symlink 逃逸。
- `moc create`：在 `05 Literature/MOCs/` 下创建 Topic MOC 笔记（`kind: topic-moc` + 空四列表格）；需要 `--vault` / `--title`（即文件名，CJK 保留）；目标已存在 → `conflict`；空标题或含路径分隔符 → `error`。受管 vault mutation：全程持有共享写锁，另一受管操作持锁时 `conflict`（rc 3，零写入——连 `MOCs/` mkdir 与 temp 文件都不发生）。业务完成后 release 失败 → `error`（rc 2），明确 operation 可能已提交且 write.lock residue 可能保留，不宣称零写。
- `mineru convert`：把某篇的 Primary PDF 经 MinerU 转成 `minerUmd_<key>.md` + 图片（落该篇 `attachments/`）；需要 `--vault` / `--key`。已有 `minerUmd_<key>.md` 时**必须**先 `--dry-run` 拿 `needs_confirmation` + `confirmation_token` 再 `--confirm-token` 执行；PDF/旧 MD/attachments 任一在执行前后变化 → `conflict`（rc 3，零写入）。不写 `figures/`、不生成 `Figure解读`。
- `config mineru set-key --stdin`：从 stdin 读一行保存 MinerU Key（Key 永不进 argv/日志/JSON 输出；只存 `~/Library/Application Support/paper-notes/config.json`，0600）；`status` 只回 `configured` 布尔；`delete-key` 幂等删除且保留 easyscholar key。
- `index rebuild`：确定性重建 `.paper-notes/library.json` 与 `citation-aliases.json`；生成文件不得手改。受管 vault mutation：先取共享写锁，再在锁内观察（build_index）、渲染并发布，两文件发布相对其它受管 writer 序列化；另一受管操作持锁时 `conflict`（rc 3，零写入——不观察、不建 temp）。atomic 仅指单个 pathname 各自的 rename/replace；两文件不是内核原子对，进程故障可能留下 partial 状态（library 新 / aliases 旧），下次 rebuild 修复。业务完成后 release 失败 → `error`（rc 2），明确 rebuild 可能已提交且 write.lock residue 可能保留，不宣称零写。
- `migrate legacy-obsidian`：只读发现 + 迁移计划（详见 `migration.md`）。`migrate --apply` 与 `migrate rollback` 是受管 vault mutation：在解析出 vault 后、任何 vault/journal 写入前持有共享写锁，另一受管操作持锁时 `conflict`（rc 3，零 vault 写、零 journal 写）；stale/锁错误 → `error`（rc 2）。注意 apply/rollback 中途检测到的其它 `conflict`（source 缺失、目标差异）**并非全程零写**：可能已写 journal 并执行自动恢复（详见 `migration.md`）；仅锁冲突保证零写。`migrate verify` 与 dry-run 保持只读、无锁。业务完成后 release 失败 → `error`（rc 2），明确 operation 可能已提交且 write.lock residue 可能保留，不宣称零写。

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
- `status`：`success` 完成；`needs_confirmation` 需要用户确认候选值（含冲突解决候选）；`conflict` 与期望状态不一致——是否零写入取决于命令与冲突检测时机（锁冲突与 pre-mutation 校验零写入；migration 等在 mutation 后检测到的冲突可能已写 journal 并执行恢复，见各命令条目）；`error` 用户/配置/校验错误。
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
5. `mineru convert` 的重转：`--dry-run` → `needs_confirmation`（`confirmation_token` + `plan` 绑定 PDF/旧 MD/attachments 状态）→ 确认后携 `--confirm-token` 执行；执行前后任何绑定状态变化 → `conflict`、零写入。

## `mineru convert` 进度流（NDJSON）

`mineru convert`（非 dry-run）是唯一**流式**命令：stdout 先是若干 NDJSON 进度行，最后一行才是标准 envelope：

```
{"type":"progress","stage":"uploading","extracted_pages":0,"total_pages":12}
{"type":"progress","stage":"processing","state":"running","extracted_pages":5,"total_pages":12}
{"type":"progress","stage":"committing",...}
{ ...标准 envelope... }
```

- 进度行含 `type:"progress"`，可含 `stage`（`uploading|waiting|processing|downloading|cleaning|committing`）、`state`（MinerU 轮询原始状态）、`extracted_pages` / `total_pages`。
- 错误路径（无 Key、无 PDF、已有 MD 未确认、stale token 等）在**任何进度行之前**以标准单 envelope 退出，插件流式解析器必须兼容"首行即 envelope"。
- 取消 = 插件终止子进程（SIGTERM→SIGKILL）；已提交的云端任务可能继续，但提交前的状态复核保证不会覆盖本地结果。

## 不变式

- 写前校验 schema；短时工作区写锁（**单一共享锁域**：所有受管 writer 共用 `<vault>/.paper-notes/write.lock`，operation 仅是 metadata）；变更先 stage、支持单 pathname 原子替换；成功后重建索引。
- 失败语义分级：**pre-mutation 校验/锁冲突**在明示的命令上零写入；**exact anchor 失败是 success-with-warning**（卡片已创建、无死链，仅 source 零写入）；**migration 等在 mutation 后检测到的 conflict** 可能已写 journal/执行恢复；**运行失败**可能留下明确报告的 partial/residue（见 Managed/cooperative writer 契约），不宣称普遍零半成品。
- 插件在别的受管操作持锁期间禁止写操作；陈旧锁移除需确认；锁是 advisory，不约束非协作 writer。
- 任何写入路径不落 active `zotero://` 链接（migration 后不再使用）、Zotero item key、EasyScholar/IF/JCI/JCR/CAS 指标字段（详见 `frontmatter_spec.md`）。

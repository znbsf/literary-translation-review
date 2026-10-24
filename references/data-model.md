# 资料库结构

[evidence-and-corpus.md](evidence-and-corpus.md) 规定每类记录“必须说清什么”；本文给出可直接建库的标准结构，供自建脚本、随附工具包和任何查看器共用。表名与字段是约定，不绑定某部作品、语言或模型。

## 原则

- **一个语料库一个 SQLite 文件**（如 `dialogue.sqlite`），工作时以只读方式打开。补充只能经过“备份 → 事务写入 → 回执”的受控流程，并保留旧行。
- **派生结果放库外。** 模型候选、检索索引、OCR 发布、复核标签各自存放，引用库内 ID，不改写原记录。
- **绑定数据，不绑定代码。** 候选记录引用来源库、原图、原文段落的 SHA-256；读取时核对这些数据。生成它的程序版本只记作溯源，不作为能否读取的条件，否则每次改代码都会让旧证据失效。
- **ID 稳定、带版本。** 段落 ID 只在其版本内唯一；跨版本引用一律写 `(edition_id, paragraph_id)`，并附原文哈希与字符偏移。
- **私有内容不出本机。** 正文、OCR 文字、图片、凭据和本机路径不进入公开仓库、日志或工程报告。

## 资格标记

模型产出永远是候选。所有候选表和库外候选都带这组整数字段，并用 CHECK 约束固定：

| 字段 | 候选的取值 | 含义 |
| --- | --- | --- |
| `requires_review` | 1 | 需要复核 |
| `accepted` | 0 | 已被采用 |
| `human_confirmed` | 0 | 人工确认 |
| `semantic_verified` | 0 | 语义已核验 |
| `reference_eligible` | 0 | 可作参考证据 |

OCR 类候选另加 `ocr_verified`、`reading_order_verified`（均为 0）。只有显式的人工复核流程可以改变这些值；模型同意、多票一致或检索命中都不能。

## 核心表（必需）

```sql
CREATE TABLE editions(            -- 一个具体版本：某书的原文本或某个译本
  id TEXT PRIMARY KEY,
  book_key TEXT,                  -- 同一作品的各版本共享，用于分组与配对
  title TEXT, language TEXT,      -- language: ja / zh / ja-zh …
  origin_kind TEXT,               -- 来源类型：source_epub / translation / scan_ocr …
  source_sha256 TEXT,             -- 原始文件摘要
  metadata_json TEXT              -- 版本来源、读取方式、已知缺损
);
CREATE TABLE paragraphs(
  edition_id TEXT, ordinal INTEGER, id TEXT, text TEXT,
  locator_json TEXT,              -- 原始位置：EPUB 条目+元素路径+偏移，或扫描页 page_id
  PRIMARY KEY(edition_id, id)
);
CREATE TABLE dialogues(           -- 对白/引用片段
  id TEXT PRIMARY KEY, edition_id TEXT, ordinal INTEGER, text TEXT,
  start_para INTEGER, end_para INTEGER,   -- 段落范围（ordinal）
  opening TEXT, closed INTEGER,           -- 引号形态与是否闭合
  context_hash TEXT,                      -- 上下文摘要，用于判断标签是否过期
  speaker TEXT, interlocutor TEXT,        -- 可为空：未知不得猜
  attribution_status TEXT,                -- unresolved / model_attributed / not_dialogue / human_confirmed …
  mode TEXT,                              -- 直接对白 / 引用 / 心理 / 书面
  evidence_json TEXT
);
CREATE TABLE alignments(          -- 正式库中的对齐（候选另放库外）
  chinese_id TEXT, japanese_ids_json TEXT,  -- 目标语对白 ↔ 原文对白 ID 列表，允许一对多
  status TEXT, confidence TEXT, method TEXT, evidence_json TEXT
);
CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT);
```

字段名沿用早期日中项目（`chinese_id`/`japanese_ids_json`）；其他语言对照样使用，含义是“目标语一侧 / 原文一侧”。

## 初始化与格式兼容

上面的 SQL 表达字段约定；实际初始化命令是：

~~~sh
python scripts/ltr corpus init <新的私有目录>
python scripts/ltr corpus check <dialogue.sqlite>
~~~

新库用 metadata 的 schema 键保存 JSON 字符串 "ltr-corpus-v1"。初始化为 ID 非空、段落/对白序号与范围、JSON 格式添加 CHECK/UNIQUE 约束，并声明版本与范围端点的外键；写入者仍须开启 PRAGMA foreign_keys=ON，对齐数组中的每个 ID 由检查器验证。没有该元数据的旧库只读兼容并报告警告；不会将验证动作变成迁移。附加字段与表允许存在。

检查核验当前核心数据与字段，不推断记录的人工或语义资格；已在 alignments 中的机器记录仍保留其候选状态。缺失或无效数据报告代码与计数，不输出正文、定位 ID 或私有路径。

## 绑定项目与旧格式排序

新版 translation-project-v2 的 project.json 不保存 source 正文；corpus_binding 记录相对数据库路径、整库摘要、版本 ID，以及按顺序选定的段落 ID/摘要。读取时重建兼容的内存对象并校验 source_digest，译文和状态仍由项目保存。

工具导入的 paragraphs.locator_json 保留原始 EPUB 定位，并在 _project_source_fields 中保留完整的非正文段落元数据（如 ruby），用于无损重建和精确绑定；正文始终只从 paragraphs.text 读取。原始 XHTML 快照及旧项目备份是溯源档案，不是另一套可变原文。

新版库的对白序号仍受非负整数约束。未标版本的旧库允许经过证据验证的 ASCII 引号补录排序键：在整数 base 后插入 n 条时为 base + k/(n+1)，也可在首条 0 前插入。兼容检查核对补录类型、原文跨度、整组公式、顺序和整数锚点，另报数量警告；不改变存量数值，不赋予任何语义或人工资格。通用未知小数仍是检查失败。

## 查询结果的资格

ltr evidence 的返回为 ltr-evidence-v1：来源与正式库分别记录摘要，每条关系标 verified/candidate/clue，逐段携带位置和文本摘要。资格只来自显式人工、语义和参考标记，不根据 alignments 的存放位置或状态字符串推断；库外建议始终是 candidate，全文命中始终是 clue。

精确定位使用半开区间 [start,end)；重复或归一化后无法唯一定位时保留段落范围，长跨度保留首尾定位且偏移为 null。未知值不由程序补齐。完整查询参数和输出限制见 [toolkit.md](toolkit.md#证据查询)。

## 可选表

| 表 | 用途 |
| --- | --- |
| `entries` | 审校工作条目：原文、译文、说话人、对齐与参考资格的扁平视图 |
| `attribution_candidates` | 说话人/听者候选（surface 原样写法 + canonical 归一名），附检测器与运行标识及资格标记 |
| `scan_collections`, `scan_pages` | 扫描书：每页 `image_path`、`image_sha256`、宽高、OCR 状态 |
| `chapter_candidates`, `edition_pair_candidates` | 章节切分与跨语种版本配对的候选，带 `status`/`basis` |
| `corpus_candidate_archive` | 经复核流程追加进正式库的机器候选：kind、来源摘要、记录 JSON、导入批次与资格标记 |
| `candidate_write_receipt_outbox` | 写库回执，与写入同一事务提交 |
| `paragraph_search` | FTS5 全文索引（trigram 适合日中文） |

## 库外候选与发布

每条库外候选至少包含：

```json
{
  "schema": "<类型>-v<n>",
  "source_sha256": "生成时来源库的摘要",
  "binding": {"edition_id": "…", "paragraph_id": "…", "paragraph_sha256": "…", "start": 0, "end": 12},
  "model": "实际模型标识", "session_id": "…",
  "requires_review": 1, "accepted": 0, "human_confirmed": 0, "semantic_verified": 0, "reference_eligible": 0,
  "provenance": {"runner": "…", "code_sha256": "仅作溯源"}
}
```

一批候选形成一次发布：候选文件或 SQLite 存储 + 摘要（计划、实际调用、有效/失败/结果未知/未尝试数、停止原因、用量）。失败与“结果未知”分开记录；未知是否成功的调用先对账再决定，不盲目重试。

## 词表（固定表达）

```sql
CREATE TABLE glossary(
  id TEXT PRIMARY KEY,
  source_form TEXT,         -- 原文写法（含注音、括号）
  target_form TEXT,         -- 工作译法
  kind TEXT,                -- 人名 / 称号 / 招式 / 固定句 / 地名 / 术语 …
  conditions TEXT,          -- 适用语境；固定句的允许变奏
  variants_json TEXT,       -- 已知异译及出处
  sources_json TEXT,        -- 原文出处与历史译本出处（书名、节次、场景）
  status TEXT,              -- proposed / confirmed / superseded
  version TEXT              -- 所属词表版本；锁定后不改
);
```

批次只共享某一锁定版本中 `confirmed` 的完整清单；`proposed` 留在队列。被推翻的条目标为 `superseded` 并保留。

## 人工复核标签

标签保存在库外（每条记录一个文件或一张独立表），至少含：记录来源与 ID、判定（correct / incorrect / uncertain）、可选说话人/听者/备注、保存时的库指纹与 `context_hash`、版本号与时间。库指纹或上下文变化后标签显示为过期，不自动迁移。

## 执行记录

与 [review-manifest](../assets/review-manifest.example.json) 一致：每阶段的实际模型、推理设置、覆盖 ID、预算上限与消耗、checkpoint 和状态（pending / running / succeeded / failed / unknown）。

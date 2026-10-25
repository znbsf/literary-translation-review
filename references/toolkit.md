# 随附工具包 ltr

`scripts/ltr/` 是本 skill 的工具包：Python 3.11+ 标准库实现（读取扫描图另需 Pillow），按 [data-model.md](data-model.md) 读写证据库，自带本机查看器。没有它时，照同一结构自建脚本即可，流程要求不变。

```sh
python scripts/ltr <命令> [参数]            # 在 skill 目录下
python -m ltr <命令> [参数]                 # 或把 scripts/ 加入 PYTHONPATH
python scripts/ltr <命令> --help            # 每个命令的参数
```

## 按工作流步骤

| 步骤 | 命令 | 作用 |
| --- | --- | --- |
| 1 建库与检查 | `ltr corpus init <新目录>` / `ltr corpus check <dialogue.sqlite>` | 新建私有空库；只读核对已有库的核心字段、ID、范围、JSON 与引用，只输出计数和摘要 |
| 1 项目绑定 | `ltr project import --corpus / bind / migrate / recover` | 新工作库导入、已有库精确只读绑定、旧项目迁移与中断恢复 |
| 1 冻结来源 | `ltr project inspect / locate / import` | 只读解析 EPUB 元数据与导航；按显式 spine 导入，建立稳定段落定位与原始快照 |
| 1 固定表达 | `ltr glossary init / propose / add / confirm / supersede / reject / lock / export / list` | 库外提案、人工裁定、不可变版本快照与共享 |
| 2 理解全书 | `ltr ledger init / add / decide / edit / remove / confirm-read / check / export / list` | 库外八维理解台账，显式阅读声明与翻译决定，导出审校清单段 |
| 3 查询证据 | `ltr evidence line / speaker / term` | 跨版本、人物与字面词条查询；已核/候选/线索分级，不写库 |
| 3 按证据对齐 | `ltr quotes` → `quotes-review` → `quotes-index` | 无引号版本的引用片段候选、独立复核、只读索引 |
| | `ltr align` / `align-plan` / `align-campaign` → `align-review` → `align-index` | 对白对齐提议、按冻结证据规划目标、有限连续会话、独立复核、只读索引 |
| | `ltr attribute` → `attribute-index` / `attribute-projection` | 说话人/听者建议（证据片段必须是原文唯一子串）及严格投影 |
| | `ltr ocr-vision plan / run` | 整页 OCR 候选 |
| | `ltr ocr-order` | 已有 OCR 的阅读顺序几何分列 |
| | `ltr ocr-regions` | 按几何区域逐块转写，每块标记已转写或不可读，写入区域存储 |
| | `ltr review-queue` | 生成带库指纹的待复核队列 |
| 4 生成与审校 | `ltr project check-context` | 校验上下文卡：证据来源、偏移与知情终点 |
| | `ltr project render / apply` | 生成原文-译文对照审校页；导入人工修改并保留历史版本 |
| 5 改后复查 | `ltr workbench --config <cfg> --port 0` | 本机查看器：概览、对白（上下文与全部机器意见）、扫描页叠框、复核标签与导出 |
| | `ltr glossary check --version <版本> --project <P>` | 按源段/译段绑定核对固定表达，只报告待复核问题 |
| 6 交付 | `ltr assemble` | 按最终正文组装 TXT/EPUB |
| | `python scripts/check_manifest.py <manifest.json>` | 核对申报的覆盖、改后复查与执行记录 |

尚未实现（规划）：source-first 上下文生成与候选冻结命令。在实现前按 [evidence-and-corpus.md](evidence-and-corpus.md) 手工记录，不要声称运行过它们。

## 八维理解台账

~~~sh
python scripts/ltr ledger init --ledger <L.json> --project <P>
python scripts/ltr ledger add --ledger <L.json> --dimension character_arcs --kind fact --locator <段落ID> --observation <观察> --decision <决定>
python scripts/ltr ledger decide --ledger <L.json> --id d1 --decision <决定>
python scripts/ltr ledger confirm-read --ledger <L.json> --by <人工声明者> --note <说明>
python scripts/ltr ledger check --ledger <L.json> --project <P>
python scripts/ltr ledger export --ledger <L.json> --manifest <M.json> --write
~~~

init 通过 read_project 读取 v1/v2 项目，按原顺序冻结段落范围和 project.json 文件 SHA-256，已有台账拒绝覆盖。台账及写入清单须在 Git 与语料目录之外；拒绝链接路径，以目录排他锁和原子替换保护写入。检测祖先目录的 dialogue.sqlite；初始化还检查项目绑定的数据库目录。并发写遇锁即拒绝，不自动重试或移除已有锁。

add 的 kind 只能是 fact（原文事实）、narration（角色叙述或信念）、interpretation（审校解释）；分类由记录者负责，程序不判断文学解释真假。省略 decision 为 pending；提供时必须非空。可用 --range <首段> <末段> 指定有序范围，locator 必须位于其中。edit 使用与 add 相同的可选字段，--pending 清除决定并改为待决，--clear-range 清除范围。remove 只标 withdrawn，保留记录与 ID；撤回项不计入 check 和 export。list 可按 --dimension / --status 筛选。

只有 confirm-read 写入全书已读声明及声明者、UTC 时间、备注。覆盖八维不会自动声明阅读；程序记录人工申报，不验证声明者身份或实际阅读。check 只读报告缺维度、定位问题、空决定、pending、未声明阅读；带 --project 时再核对摘要与范围。退出码为 0 无问题、1 有问题；其他命令的参数或环境拒绝为 2。错误只输出静态码，不回显私有文本或路径。

export 默认打印 whole_book_understanding，--manifest 指定目标但默认不读取或写入清单；--write 更新已有清单，逐字节保留目标值以外的内容，目标段缺失则追加。dimensions 包含非撤回条目实际覆盖的维度，decisions 仅包含 decided 项。pending 和未声明阅读允许如实导出，仍由 check 与 check_manifest 报告不完整；结构损坏的记录拒绝导出。清单通过不代表文学质量或人工终审。

## 建库与只读检查

运行命令（目录的父目录须已存在）：

~~~sh
python scripts/ltr corpus init <新的私有目录>
python scripts/ltr corpus check <已有目录/dialogue.sqlite>
~~~

初始化拒绝已有目录、Git 工作区及符号链接/联接点，创建带约束的五张核心表与 schema 元数据。不写入现有正式库；书稿导入与迁移使用下节的显式项目命令。

检查只读打开数据库，执行 SQLite quick_check、必需表/字段、ID/序号唯一性、段落范围端点、JSON 和对齐引用检查。检查前后完整计算 SHA-256；存在 WAL/SHM/journal 或路径链接时拒绝。输出不含正文、记录 ID、私有路径或原始 SQLite 错误。退出码：0 通过，1 结构/数据检查失败，2 参数或环境拒绝。

没有 schema 标记的旧库按核心数据规则检查，并给出 schema_version_missing 警告，不补写版本。旧库 ASCII 引号补录若具有完整来源跨度，且每组严格符合 base + k/(n+1) 的插入次序，可保留有限小数排序键并报告 legacy_fractional_dialogue_ordinals；未知来源、损坏槽位和新版库的小数仍拒绝。工作台保持实际数值，不作取整。未知标记拒绝；附加表/字段保留。检查不证明任意旧库具备新库的所有写入约束，也不核验对齐语义、模型候选资格、来源真实性或文学质量。正式库中的 candidate / model_matched / exact_source_id 不能仅因存储位置升级为已核证据。

## 项目导入、绑定与迁移

不传入 corpus 参数的旧导入仍生成 v1 项目。新绑定项目把正文只放在 SQLite 中，project.json 保存段落选择、内容摘要、译文与修订状态；read_project 按需校验并加载来源，旧编辑/预览接口继续可用。

~~~sh
python scripts/ltr project import <source.epub> --items <spine-id> --project <P> --corpus <P/corpus>
python scripts/ltr project migrate --project <P> --corpus <P/corpus>
python scripts/ltr project bind --project <P> --corpus <existing/dialogue.sqlite> --edition <edition-id>
python scripts/ltr project recover --project <P>
~~~

- import 的 P 必须是新目录；import/migrate 的新库必须是 P 的直接子目录且尚不存在。不会写已有库、修改正式库、更新共享配置或自动导入模型候选。
- migrate 迁移当前 v1 项目：当前译文、状态、修订号、上下文与 issues 保持，已有 history 文件逐字节保留；原 project.json 另作逐字节备份。新 v2 修订的历史只保存库引用，不再复制正文。
- bind 只读绑定已经存在、具有本工具段落元数据的语料库；段落 ID、正文和完整定位元数据必须与 v1 项目精确一致。不会猜测 ID 映射、替换原文或向库中补写元数据。不满足条件时拒绝，不能将近似文本匹配当成绑定。
- 新库提取日文角引号片段（支持嵌套、跨段和未闭合）；只记录原文跨度，归属保持 unresolved，不授予人工、语义或参考资格。没有角引号时仍可在工作台“版本”列表看到版本，不伪造对白。
- SQLite 在发布后视为不可变：每次项目读取核对完整文件摘要、逐段摘要与原始快照；来源变化即拒绝。数据库和项目在同一目录树时可整体移动；外部绑定路径相对项目记录，移动前须保持相对位置。
- 构建前记录 pending 意图，先校验新工作库，最后原子替换 project.json 作为发布点。中断前仍是可读的旧 v1，或尚未发布的新目录；中断后已有 pending 时用 recover 完成。已有库不被重建，未完成暂存文件保留并列入私有回执，不自动删除。首次准备记录出现之前的文件创建失败只会留下未发布目录，不称为已完成导入。
- pending 存在时阻止其他编辑；恢复时核对旧项目摘要/已发布的新项目、来源和快照，冲突拒绝。项目已继续编辑后，旧发布回执不能用来倒填恢复。

回执、原始 XHTML 快照、项目、工作库及未完成暂存都属于私有书稿材料，须放在 Git 外。结构和绑定通过不等于阅读覆盖或文学验收。

## 项目参考窗口与交付

`ltr project reference-window --help` 查看显式选择既有译本窗口的参数。窗口写入新的私有文件，不声称已对齐。

`ltr assemble --project <P> --out <私有目录> [--format epub|txt|both]` 从 v2 书稿项目按源段顺序组装，默认同时输出 TXT 和 EPUB。每段必须有非空且 accepted 的译文，否则失败并报告段 ID。显式 `--allow-draft` 可输出未确认稿，首页标注“含未确认草稿”，空白或缺失译文显示“〔缺失译文〕”，不填入原文。

输出目录必须在 Git 外的私有根下；已有文件一律拒绝覆盖，每个文件原子发布。成品使用读者编号，不附带内部段 ID、源定位或本机路径。输出为纯文字版，不包含原书图片；结构成功不代表文学终审或阅读器验收。

读者版卷首简述原稿、既有译稿及所用 AI，工程账本留在私有附件。目录使用实际节次，译注保留往返链接，EPUB3 导航与 NCX 目录保持一致；引用以书名、实际节号及可识别场景呈现，未核实章节须明示。购买入口须核对版本、作者及纸书/电子书，封面优先使用实际原稿封面，缺失时再取自已核实商品页，不生成替代封面。前作证据须对应具体翻译决定，读者背景不等于角色当时知情；先独立形成译法再比较现译。人工终译、术语确认和发布授权不能由工具成功替代。

## 证据查询

~~~sh
python scripts/ltr evidence line --config <cfg.json> --corpus <id> --dialogue <dialogue-id>
python scripts/ltr evidence speaker --config <cfg.json> --corpus <id> --name <name> --book <book-key> --addressee <recipient>
python scripts/ltr evidence term --config <cfg.json> --corpus <id> --text <literal> --lang ja
~~~

三类命令与工作台 GET /api/evidence 共用同一个只读查询函数：

- line 支持原对白、alignment: / identity: / candidate: 详情 ID、反向检索和一对多关系；各旧译保持独立，不做多数票。
- speaker 可按作品和对象筛选。别名来自同一来源中的 surface/canonical 记录，仅作为搜索键；排除当前稿及作品范围外的别名。人物索引会按既有验证器重校验，冲突观察不合并成确定身份。
- term 使用字面子串检索，百分号和下划线也按字面处理。每个命中始终为 clue，相关对白的独立对应关系另列在 correspondences 中，不能因同段出现就当成对应。
- 工作台新增“证据”视图，提供三种查询、筛选和分页；对白详情新增惰性加载的“证据”页签，可以跳到该人物的其他台词。

每条关系都有 level：

| level | 展示 | 要求 |
| --- | --- | --- |
| verified | 已核 | 正式关系的字段或 evidence_json.qualification 中 human_confirmed、semantic_verified、reference_eligible 均为整数/布尔真，且 requires_review 未标为待复核；错误类型或缺失资格不升级 |
| candidate | 候选 | 正式库中尚未具备上述资格的记录，或库外对齐/归属建议；独立复核的保留、拒绝、不确定意见分别保留，均不升级 |
| clue | 检索线索 | 全文子串命中；即便另列已核关系，命中本身仍保持 clue |

不以表的位置、matched 字样、置信分数或模型一致判定已核。未知 origin_kind 与 generated_draft 等当前稿不参与查询；原文、历史译本和明确的来源类型按白名单准入。查询不授予任何资格。

返回版本标题、人物归属状态、(edition_id, paragraph_id)、段落 SHA-256 和半开字符偏移。无法唯一定位时显式标为 paragraph_range；超过 100 段时只返回首尾定位与 endpoint_range，字符偏移为 null，不伪造精确匹配。来源库和正式库摘要分别记录；已有源快照/索引的摘要和逐条来源绑定照旧校验，异常拒绝，不静默降级为“查无结果”。

默认短摘 160 字，可用 --excerpt-chars 设置 0–400；默认每页 20 条，--limit 为 1–100，--offset 不超过 10000。line 单一起点最多处理 1000 条关系，超限拒绝。term 每个命中最多附五条关系，另报可能截断，完整关系可进入对白继续查询。

完整正文须显式 --with-text；每条最多显示 100000 字，仍超出时保留 text_truncated 和原长度。默认只写终端，不写请求或正文日志。--output <new.json> 仅允许新文件，必须在 Git 和所有语料目录之外；源库、现有文件和不安全路径均拒绝。工程验证只记录计数、摘要和错误码，真实正文留在本机内存或显式私有输出。

## 固定表达词表

所有命令都必须带 `--store <glossary.sqlite>`。父目录须已存在，存储必须在 Git 工作区及所有语料目录外；拒绝符号链接、联接点、硬链接和未完成的 SQLite 日志。无配置的命令按祖先目录中的 .git / dialogue.sqlite 检查；propose 另核对配置中全部语料根并记入私有 meta，后续访问继续检查这些根。工具不扫描磁盘寻找未声明的任意语料目录，使用者仍须将词表放在明确的独立私有目录。

~~~sh
python scripts/ltr glossary init --store <S>
python scripts/ltr glossary propose --store <S> --config <cfg.json> --corpus <id> --book <book-key> --limit 100 --min-count 2
python scripts/ltr glossary propose --store <S> --config <cfg.json> --corpus <id> --from-json <既有词表.json>
python scripts/ltr glossary add --store <S> --source <原文写法> --target <工作译法> --kind <类型> --conditions <适用条件>
python scripts/ltr glossary confirm --store <S> --id <条目ID> --target <确认译法> --note <理由>
python scripts/ltr glossary supersede --store <S> --id <旧ID> --by <新ID> --note <推翻理由>
python scripts/ltr glossary reject --store <S> --id <条目ID> --note <拒绝理由>
python scripts/ltr glossary lock --store <S> --version v1 --note <范围说明>
python scripts/ltr glossary export --store <S> --version v1 --format json
python scripts/ltr glossary export --store <S> --version v1 --format md
python scripts/ltr glossary list --store <S> --status proposed
python scripts/ltr glossary list --store <S> --version v1
python scripts/ltr glossary check --store <S> --version v1 --project <P>
python scripts/ltr glossary check --store <S> --version v1 --pairs <pairs.json>
~~~

- init 拒绝已有文件；add 的 target、conditions 可省略，但始终新增提案。propose 只读原文版本（japanese_source/source_epub/source_text），排除历史译本与当前生成稿；有源快照配置时沿用其绑定校验。按完整段落提取『』【】〈〉《》内 1–80 字片段和至少出现两次的连续片假名（2–40 字）/汉字（2–20 字）短语。逐条记录实际出处与次数，已有 source_form 不重复，limit 只限制新提案数（1–10000）。这不是分词或专名识别，跨段拆开的表达及其他语言需要人工 add。
- `--from-json` 接受条目数组，或含 entries 数组的导出对象；字段为 source_form、可选 target_form/kind/conditions、variants_json/sources_json（也接受 variants/sources 数组）。导入状态、ID、裁定被忽略，只生成 proposed，出现次数未知时记 0。括号、重复和导入都不授予确认资格。
- confirm 必须有非空 target。已确认条目不能直接改译法；新建同源提案并确认后，用 supersede 保留旧译与理由。替代项必须已确认，禁止自指与循环；reject 同样要求理由。终止条目不重新确认。
- lock 保存当前全部 confirmed 的完整快照及当时的旧裁定历史；空确认集、重复版本拒绝。工作台会列出同源多个 confirmed 冲突，锁定本身不代替语境裁定。后续编辑不改变旧版本内容、导出、摘要或核对依据。export 只能读取锁定版本，JSON/Markdown 输出到终端；真实内容的重定向目标也应在私有目录。
- JSON 导出的 confirmed_glossary 可直接填入 manifest，默认 complete_for_scope=false，必须由人确认范围完整性后设为 true。`check_manifest.py` 核验申报，不能据此证明原文覆盖或文学准确。
- pairs.json 为 `[{"source":"自编原文","target":"自编译文","locator":"p1"}]`。project 使用项目实际段落 ID 绑定译文；v2 原文从绑定工作库读取并核对摘要，不按数组位置猜配。旧 v1 项目保留其已有源译绑定。check/list/export 不创建或修改文件。
- check 检查绑定段落内源词出现而确认译法（或显式允许变体）缺失、被推翻旧译残留，以及同源词出现多个已知译法；多译法仅匹配词表已知项，不从整段译文推断未知译名。没有 source（或为 null）的行只报译文字符串线索，并明确“未完成源词覆盖检查”；混合输入也不会宣称全范围覆盖完成。
- ordinary/common/unclassified/普通词/普通同形词/普通引用/未分类，以及有 conditions 的条目，仅报 clue；工具不执行自然语言条件，也不自动把普通同形词判成专名。其他 finding 也只是字面异常，requires_review=true，不证明语义误译。子串重叠、重复次数、拆分表达与文学变奏仍须人工核查；不执行替换。
- 工作台新增只读“词表”页，`GET /api/glossary?corpus=<id>[&version=<版本>]` 返回版本列表、所选确认快照、当前提案和冲突/替代链；默认选最近锁定版本。未配置时显示说明，工作台没有词表写入接口。

退出码：0 表示命令完成（check 有待复核问题也返回 0），2 表示参数/存储/来源拒绝。错误只返回静态代码，不回显输入、路径或正文。所有词表命令零模型调用、零网络调用。

## 查看器配置

把配置文件放在书稿与仓库之外：

```json
{
  "corpora": [{
    "id": "book", "name": "某系列", "path": "../data/book-corpus",
    "candidate_index": "…/quote-index.sqlite",
    "alignment_index": "…/alignment-index.sqlite",
    "attribution_index": "…/attribution-index.sqlite",
    "glossary": "…/glossary.sqlite",
    "ocr_vision_candidates": "…/vision-run/",
    "ocr_reading_order_candidates": "…/reading-order/",
    "ocr_regions": "…/regions.sqlite",
    "run_snapshot": "…/run-snapshot.json",
    "bibliography": "…/publisher-bibliography.json",
    "source_snapshot_db": "…/dialogue.source-<sha>.sqlite", "source_snapshot_sha256": "<sha>"
  }],
  "review_dir": "…/reviews"
}
```

除 `id`、`path`（含 `dialogue.sqlite` 的目录）外都可选；每个证据路径必须在所有语料目录之外。服务只监听 `127.0.0.1`，只读打开语料库，复核标签写入 `review_dir`。

## 模型调用规则

语料证据候选命令（quotes、align、attribute、ocr 等）的模型调用规则如下；project 阶段工作流使用上节说明的独立入口与授权边界：

- 必须显式加 `--execute`；默认只做计划与预检。
- 一个进程一个会话，串行调用，历史只在内存；思考块按原样回传以维持会话。
- 调用次数、请求字节、输出 token 预算是硬上限，超出前停止。
- 第一个失败即停止，不自动重试；“结果未知”与“失败”分开记录。
- 每次调用前后复核来源库与图片摘要，变化即停止且不发布该批结果。
- 认证只从环境变量读取（`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`，Messages API 格式的网关均可）；模型由参数指定，不写入仓库。
- 输出目录必须新建且在语料目录之外；只写候选与不含正文的摘要。

## 其他工具

`python scripts/ltr tools <子命令> [参数]` 是维护工具统一入口，也可从 scripts/ 运行 `python -m ltr.tools <子命令>`。参数原样传给对应工具。

| 子命令 | 作用 |
| --- | --- |
| manifest | 生成语料清单和摘要，不修改数据库 |
| leak-scan | 扫描语料泄露；沿用 --root / --corpus / --declared 参数 |

~~~sh
python scripts/ltr tools --help
python scripts/ltr tools manifest <私有语料目录>
python scripts/ltr tools leak-scan --root <数据根> --corpus <语料目录> --declared <豁免清单.json>
~~~

--declared 为可选参数；未使用豁免时省略。清单、候选和私有窗口均不是文学质量或正式库资格的证明。

部分候选 schema 名沿用最早实践项目的命名（如 `haruhi-alignment-candidates-v1`），是格式标识而非作品限定；已发布的数据按原名校验。

## 测试

```sh
python -m unittest discover -s tests -t .
```

全部使用合成数据，约一分钟。`tests/ltr/` 与 `scripts/ltr/` 包结构一一对应。

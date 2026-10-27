# 独立作品理解与系列资料整理

用于用户要求理解整部作品、整理系列资料或可追查的阅读指南，即使没有翻译任务。先确认作品、卷册、语言和版本、阅读范围、成果形式与剧透边界；检查已有资料及断点。源文件与派生资料分开，保留缺页、OCR、版本差异和未获得的卷册。以用户实际授权且可获得的语言版本为底，不默认必须使用日文。译文可作为所整理版本的直接来源，但其内容不能冒充已核原作；第三方梗概和检索结果只能作为标明局限的线索，不能冒充已读原文。

## 阅读与组织

事件索引可以概述一个完整场景，但人物信念、规则和因果等可争议命题应尽量一条一义，独立注明说话者、时点与依据。不要为减少记录数把否定、传闻、后续结果压成一句确定结论；出现误读时修订相邻同类项，保留旧记录与修订理由。 复核和修订须同时检查命题、story_time、disclosed_at、分支、证据层级及关系端点；不能只改 statement 而留下与之矛盾的辅助字段。派生指南、接续包和页面用修订后的同一版本重建。

先按原文范围逐卷、逐场景阅读并建立定位；资料不完整时明确停在实际范围。阅读当块时保留场景边界、原始位置和极短原文锚点，生成记录时回查所引范围，不在卷末凭摘要或记忆估算行号。范围在文件内合法不代表内容支持命题；若抽检发现跨场景错位，扩大检查到同一生成批次的记录与关系，并同步修复依赖它们的交接包和系列结论。按需要记录：

- 事件和场景、参与者、地点、物件、发生条件、结果和因果；分别保存出版顺序、叙述呈现顺序、故事内时间顺序。时间不明、互相矛盾、平行分支或假设顺序保留为候选，不强排成唯一时间线。
- 人物身份、共同经历、关系变化、弧光阶段、动机和面向不同对象/情境的声线；把动机解释与文本明确陈述分开。记录状态适用的时间与视角，避免后卷知识倒灌早期。
- 读者、叙述者和各人物在某时点知道、相信、不知道或误知什么；区分叙述可观察内容、角色说法和解释。对不可靠叙述保留不同解释及支持、反证，不自动把角色陈述当事实。
- 世界规则及其条件、例外和变化；伏笔、悬念、回收及仍未解决的问题；跨卷人物、事件、设定与称谓的连续性和冲突。区分真正矛盾、视角差异、版本差异与证据不足。
- 主题、意象、重复与互文的具体出现和可能联系；空间、重要物件的状态与流转；版本、来源和定位。主题与互文是可争议的解释，不靠关键词共现直接定论。

覆盖矩阵按卷/篇、章节或场景及维度列出实际范围、缺口和状态；不适用须给理由。维度键为 events、characters、relationships、chronology、causality、knowledge、narrative、world_rules、foreshadowing、continuity、themes、space_objects、versions。声线和固定表达归 characters 或 narrative；叙述视角、节奏、距离和风格归 narrative；注释与版本来源归 versions，保留原翻译八维所需信息。按用户目标确定适当粒度，长作品可先按场景建立索引再针对争议追踪句段。保持作品阅读顺序与整理顺序可分离，提供可回查的证据及剧透控制。

## 精简 JSON 契约

以下是可扩展的交换结构，不要求改造现有 `ltr-ledger-v1`、数据库或宣称已有工具支持。ID 在项目内稳定；所有 locator 指向同一已登记版本的实际位置，范围采用明确的定位约定，无法定位填 null 并说明原因，不编造。哈希使用源文件原始字节 SHA-256；文本抽取或修订另记录派生摘要、方法与版本，不用派生摘要冒充原始文件。示例仅展示结构，不代表完整覆盖：

```json
{
  "schema": "work-understanding-v1",
  "scope": {"works": ["work-1"], "volumes": ["vol-1"], "limits": "指定卷册"},
  "sources": [{"id": "src-1", "work_id": "work-1", "volume_id": "vol-1", "edition": "版本标识", "language": "ja", "origin": "来源说明", "sha256": "源文件SHA-256", "extraction": {"method": "文本提取", "sha256": "派生文件SHA-256"}, "limitations": []}],
  "events": [{"id": "evt-1", "source_refs": [{"source_id": "src-1", "locator": "章节/页或段落范围"}], "summary": "事件摘要", "participants": ["person-1"], "place": null, "objects": [], "publication_order": 1, "narrative_order": 1, "story_time": {"value": null, "branch": "branch-1", "status": "unknown"}, "conditions": [], "status": "candidate"}],
  "person_states": [{"id": "state-1", "person_id": "person-1", "at_event": "evt-1", "relationship_context": null, "experience": null, "arc": null, "motivation": null, "voice": null, "knowledge": [{"subject": "某事", "state": "unknown"}], "source_refs": [{"source_id": "src-1", "locator": "章节/页或段落范围"}], "status": "candidate"}],
  "relations": [{"id": "rel-1", "type": "causes", "from": "evt-1", "to": "evt-2", "condition": null, "source_refs": [{"source_id": "src-1", "locator": "章节/页或段落范围"}], "status": "candidate"}],
  "claims": [{"id": "claim-1", "kind": "interpretation", "subject": "person-1", "text": "有依据但可争议的解释", "speaker_or_viewpoint": null, "at_event": "evt-1", "source_refs": [{"source_id": "src-1", "locator": "章节/页或段落范围"}], "counter_refs": [], "status": "candidate"}],
  "coverage": [{"volume_id": "vol-1", "range": "第一章", "dimension": "events", "machine_read": true, "human_confirmed": false, "gaps": [], "note": "机器处理范围不等于人工阅读"}]
}
```

`relations.type` 可用 causes、enables、prevents、precedes、contradicts、supports、foreshadows、resolves、knows、believes、relationship_change、variant_of；记录方向、条件、时间/分支和证据，必要时扩展类型。`claims.kind` 使用 fact（文本可观察事实）、character_statement（角色说法或信念，须给说话者）、interpretation（整理者解释）；叙述者说法也应记录视角，不能自动等同客观事实。状态至少区分 candidate、human_confirmed、uncertain、rejected；保留反证、理由和旧记录。`human_confirmed` 是对指定记录或范围的人工作出的核验，不能由模型生成、计数或程序检查自动置真；覆盖矩阵的 `machine_read` 仅表示机器实际处理该范围，二者可独立为真或假。记录人工阅读声明的范围、版本、确认者和时间；实际未阅读原文时不得声称原文通读。处理所有段落或覆盖所有维度也不能单独证明全书理解。

## 经济分工与质量控制

经济性按首次读取、复核和返工的总投入判断，不能只比较模型单价。低价模型在多批次反复出现同类系统性错误时，缩小生成单位、强化当场证据锚点或升级该环节；已生成的同批候选扩大到受影响项核查，不以继续堆量代替修复。

确定性任务（定位、去重、排序、摘要绑定、覆盖计算）优先用本地程序；模型按场景或章节生成候选，输入限于必要上下文，记录实际模型、版本、范围、预算和检查点。分批复用已验证结果，避免全历史资料无差别扫描。模型候选与正式确认分开，人工抽检应覆盖不同卷、人物、叙述视角及高风险类别，并包括随机和针对性样本；记录抽样范围和结果，不把抽检外推为全量确认。对因果、时间分支、身份、知识状态、跨卷冲突和高影响解释优先回原文核查；若抽检发现系统性错误、来源缺损或冲突无法解释，扩大复核范围、修正生成方法并重新检查受影响项。无法解决时标记不确定和缺口，不强行闭合。

交付报告区分原文实际阅读、机器处理、人工核验及覆盖缺口；标明版本差异、未解决争议、剧透范围及候选状态。不得把结构通过、模型自评、段数或抽样通过称为完整理解或全文人工核验。

## 单源记录适配与验收

`disclosed_at` 表示本条完整命题在所用版本的叙述中已有足够信息成立的位置，不是场景开始或第一处证据的位置。跨时点、跨人物知识的复合命题优先拆分；不能确定时填 null 并说明，不机械取范围首行或末行冒充精确揭示时点。整理者事后综合必须注明其依赖的后文，不能据此推断人物此前已知。事件的故事时间另存，不能用披露位置代替。

用户只提供片段而无文件时，可以先用临时来源 ID、用户给出的行号和“待绑定”状态整理，源哈希暂为 null。此类记录不应阻断阅读分析，但不能通过下方的文件来源绑定检查，也不能声称已覆盖整卷；获得文件后另做定位和摘要绑定，不伪造摘要。

单源任务可以使用更简单的 `records` 形式：顶层包含 `schema`、`work_id`、`source`（`edition_id`、`sha256`、`path`、`language`、`locator_kind`）、`reading`（`machine_read_ranges`、`human_confirmed`、`limitations`）、`records`、`relations`、`coverage` 和 `questions`。每条 record 至少有 `id`、上述 `dimension`、`kind`、`statement`、`evidence`、`entities`、`story_time`、`disclosed_at`、`uncertainty` 和 `status`；关系至少有 `id`、`from`、`to`、`type`、`statement`、`evidence`、`kind` 和 `status`。单源时 evidence 可仅保存该源内的范围，例如原始 TXT 的 1-based 闭区间 `start_line` / `end_line`，须注明定位约定。多源或跨卷时显式携带来源 ID，确保每个端点能唯一定位。 新候选优先在阅读当块时保存极短的原文 `evidence.anchor`，检查器验证它逐字存在于所引行范围；旧记录可不带此字段，不为凑字段全库重建。锚点须来自当时实际阅读的依据，不能事后从任意范围自动摘一段充当验证；逐字匹配仍不证明该片段支持命题。

此结构将事件、人物状态和 claims 映射到 records 的相应维度，以 relations 表达它们之间的关系，source 映射 sources 中的单一来源，reading 与 coverage 共同描述处理和核验状态；不得因合并结构丢失事实、角色说法和解释的区分。模型输出初始均为 candidate，人工核验后才可改变确认状态。来源 language 按实际版本填写，中文资料不能标作日文原文。

交付前执行并记录以下检查，失败项留作缺口而非自动修正证据：

1. 每份来源的原始字节哈希与清单匹配；所有记录和关系 ID 唯一，引用端点存在；证据范围按声明的定位约定有效、非空且未越界，并能回到对应来源。
2. 覆盖矩阵逐范围、逐维度标为 `partial`、`not_started`、`candidate_covered` 或 `not_applicable`，后者必须给理由；记录支持该状态的 record ID 和缺口。一句泛泛总结不能证明某维度覆盖，`candidate_covered` 只表示候选已覆盖范围，不代表人工确认或理解正确。
3. 因果边须给出两端事件各自的来源依据及支持因果或条件的依据；先后关系 `precedes` 不自动等于 `causes`。不确定、反证与不同时间分支保留为候选或问题。
4. 分别记录覆盖范围内的全文处理证据（源哈希、实际处理范围和缺失）与理解复核证据（复核人、范围、方法、发现和未决项）；机器遍历、段落计数或结构有效不能替代阅读和理解复核。

单源 `work-understanding-records-v1` 可运行 `python scripts/check_work_understanding.py input.json --source-root ROOT [--manifest SOURCES_JSON]` 做只读结构、定位和哈希检查；它不证明阅读或理解，也不检查或替代旧 ledger。

候选检查器位于 [scripts/check_work_understanding.py](../scripts/check_work_understanding.py)，合成样本测试位于 [tests/test_work_understanding.py](../tests/test_work_understanding.py)。该检查器的定位类型固定为 `source_txt_line_1based_inclusive`；可选 manifest 的 `sources` 项须包含 path、sha256、edition_id、language、work_id，可提供 line_count。

## 顺序处理与缓存

按叙述顺序逐块追加处理同一卷；相邻且有依赖的卷优先由同一 worker 连续处理。公共规范、模式和来源前缀保持稳定，动态任务放在后面，避免无必要地改写或重排缓存前缀。一次阅读同时提取书内相关维度，不按维度反复通读全文。 分块以工具实际返回且未截断的内容为准；异常超长行应保留原行号并附字符区间分段读取，直到完整覆盖，不能仅凭请求范围或成功退出声称全行已读。

把前卷事件、人物状态和未解项压缩成带来源定位的接续包，仅对疑点回读。接续包记录其依赖的候选文件哈希；复核改变事实、归属、时点或知识状态时同步更新接续包，向顺读者追加简短修订说明，避免错误摘要继续传播。并行按独立篇或连续卷段分工；段间依赖待前置资料就绪后定向复核。处理顺序不等于故事时间顺序或人物当时的知识边界。

遵守宿主实际能力，不为缓存切换未经授权的 API，也不声称缓存可跨模型共享。记录实际 cache_read / cached_tokens；不可用记 unknown，估算与账单分开。上下文过长导致收益递减时，根据实际证据建立带来源的断点摘要，不为缓存无限累加。

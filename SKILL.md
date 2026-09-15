---
name: literary-translation-review
description: Review or retranslate book-length literature from the source, including source-first new candidates made before comparison, complete-book understanding, prior-work evidence, dialogue and fixed-wording consistency, and annotated reader editions. Use for full-book revision, retranslation, and cross-volume consistency work, not ordinary sentence translation or imitation of an author's new work.
---

# Literary Translation Review

把文学判断、可追溯资料和成品验收连接起来。保留用户指定的全文范围、文风与版本要求，不把检索、抽检或结构通过说成全文校对完成。

## 开始前

确认来源、目标版本、任务模式（审校或新译）、既有译本、相关前作、交付格式和已授权的发布/发送范围。先检查已有结果与断点。书稿与工具代码分别存放；公开工具包只含通用规则、模板、代码和自编示例。

记录实际运行模型、推理设置、资料覆盖、checkpoint、用户给出的预算硬上限及停止条件；不预设供应商或并发数。聊天模型和批处理模型可能不同，不能从界面设置推断后台模型。确定性的搜索、去重、ID核查和组装用本地程序。

## 工作流

1. **冻结来源、范围与词表。** 建立版本清单、稳定段落ID和定位；完整共享已确认名称与固定表达并锁定版本。提案与确认项分开，后续建议不得回退已确认裁定。
2. **先理解全书。** 完整阅读本书后建立八维理解和翻译决策台账，再按实际问题反查相关前作；不要先扫描全部历史库。每条理解必须指向具体翻译决定。详细字段见 [references/evidence-and-corpus.md](references/evidence-and-corpus.md)。
3. **按证据对齐。** 检索命中只是候选；核对原文、人物、语境和版本后才能称为对应。区分读者知识与人物当时所知，并分开记录原文事实、角色叙述和审校解释。
4. **生成或审校。** 新译或需要独立重译时，使用原文、全书理解及已核实的历史证据组织干净上下文；先冻结 source-first 新候选，再与当前译稿或旧译比较。审校既有译稿时先查意义，再查声线、固定译法和文学装置。
5. **改后复查。** 保留原译、建议、采纳理由和新译。复查所有改动与必要前后文，并同步译注。正确原译允许保留，不能为了体现工作量强制修改。
6. **解释与交付。** 制作说明根据实际记录填写；正文、译注和评论明确分开。分别完成全文复核、结构核验和实际阅读检查后再发送。

## 文学判断

重点检查否定、可能/必须等语气、数量、程度、时间、因果、条件、主语和指代。跨页完整句应合并理解，源图优先于存疑OCR。源文改动必须有独立依据。

人物声线随说话对象、情绪、时期和身份变化。保留有意重复、绕远、反讽、改口、错音、双关及意象照应；不要把这些一概当作“AI味”删除。模型归属不能替作者确认伪装或尚未揭晓的身份。

固定句匹配需要同时检查源表达和语境：不能因关键词出现就补出整个口头禅，不能把普通同形词全部替换成专名。工作译法不称为官方译法，除非确有依据。

输入例句证明某个表达可供参考，不证明它已在目标段落实际采用。需要 source-first 新候选时，不得把当前书中文稿、改写建议、旧译摘录或从旧稿抽取的中文场景摘要放入生成上下文。完整且已确认的名称/固定表达清单是明确例外，但其中不得夹带当前句子的整句译文。记录实际进入候选生成的资料类别、标识或摘要、排除项、词表版本和冻结checkpoint；干净的候选输入不能证明操作者或主会话从未见过旧译。

## 进一步资料

- 全书理解、干净候选、建库、对齐、错误分类、执行记录与失败恢复：读 [references/evidence-and-corpus.md](references/evidence-and-corpus.md)。
- 卷首、卷末、来源分级和资源页：读 [references/edition-layout.md](references/edition-layout.md)，按 [assets/edition-template.md](assets/edition-template.md) 填写。
- 成品旁证清单可使用 [assets/review-manifest.example.json](assets/review-manifest.example.json)。运行 `python scripts/check_manifest.py <manifest.json>` 检查申报的一致性；它不会阅读原书，也不会证明翻译准确。

## 完成报告

分别报告：全书阅读与八维理解覆盖、来源覆盖、已审段落、人物未知项、候选/确认对齐、词表版本、source-first 候选冻结与对比、改后复查、译注、EPUB结构、实际阅读和交付状态。记录实际模型、覆盖、预算消耗和checkpoint。测试PASS、抽检和模型自评不是全文校对或人工文学终审。没有子agent时可按文件隔离顺序在单会话执行；使用子agent也不能将并发角色称为模型或进程隔离证明。若尚有未完成范围，说明实际进度，不替用户缩减目标。

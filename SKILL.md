---
name: literary-translation-review
description: Review book-length literary translations against source text and prior translations, maintain character dialogue and fixed-wording evidence, and prepare annotated EPUB editions with transparent methods and source-backed commentary. Use for complete-book revision and cross-volume consistency work, not ordinary sentence translation or imitation of an author's new work.
---

# Literary Translation Review

把文学判断、可追溯资料和成品验收连接起来。保留用户指定的全文范围、文风与版本要求，不把“索引完成”说成“校对完成”。

## 开始前

确认当前任务的来源、目标版本、既有译本、交付格式和已授权的发布/发送范围。先检查已有结果与断点，避免重读重译。书稿与工具代码分别存放；公开工具包只含通用规则、模板、代码和自编示例。

记录实际运行模型、推理设置、资料覆盖及停止条件。聊天模型和批处理模型可能不同，不能从界面设置推断后台使用了哪个模型。先用短批次取得质量和消耗数据，再在阶段预算内推进。确定性的搜索、去重、ID核查和组装用本地程序。

## 工作流

1. **冻结来源和覆盖范围。** 建立版本清单、内容摘要、稳定段落ID及定位。完整重复版可凭全文一致证明复用，但保留每版出处；不同版本独立处理。缺原文、文件损坏、有资料但未读分别记录。
2. **通读与提取。** 完整阅读指定正文和旁白，同时提取人物归属、称呼、术语、口头禅、固定句式和整句台词。引用词语不等于对白，被称呼的人不等于说话人；不靠交替发言猜身份。
3. **对齐与裁定。** 日中候选支持一对多、多对一；相似度不是对应证明。旧译不是权威投票，原句、变体、出处和冲突都要保留。缺日文不能通过逆译补造。
4. **正文审校。** 先检查意义，再检查声线和固定译法。每批提供完整已确认工作词表及相关历史例证，保留语境适用规则。不要把全部历史书稿反复放入每个提示。
5. **改后复查。** 保留原译、建议、采纳理由和新译。复查所有改动与必要前后文，并同步译注。正确的原译允许保留，不能为了体现工作量强制修改。
6. **解释与交付。** 制作说明根据实际记录填写；卷末评论有论点、原文依据和不确定性。正文和评论明确分开。完成EPUB结构核验及实际阅读检查后再发送。

## 文学判断

重点检查否定、可能/必须等语气、数量、程度、时间、因果、条件、主语和指代。跨页完整句应合并理解，源图优先于存疑OCR。源文改动必须有独立依据。

人物声线随说话对象、情绪、时期和身份变化。保留有意重复、绕远、反讽、改口、错音、双关及意象照应；不要把这些一概当作“AI味”删除。模型归属不能替作者确认伪装或尚未揭晓的身份。

固定句匹配需要同时检查源表达和语境：不能因关键词出现就补出整个口头禅，不能把普通同形词全部替换成专名。工作译法不称为官方译法，除非确有依据。

## 进一步资料

- 建库、对齐、增量与失败恢复：读 [references/evidence-and-corpus.md](references/evidence-and-corpus.md)。
- 卷首、卷末、来源分级和资源页：读 [references/edition-layout.md](references/edition-layout.md)，按 [assets/edition-template.md](assets/edition-template.md) 填写。
- 成品旁证清单可使用 [assets/review-manifest.example.json](assets/review-manifest.example.json)。运行 `python scripts/check_manifest.py <manifest.json>` 检查申报的一致性；它不会阅读原书，也不会证明翻译准确。

## 完成报告

分别报告：来源覆盖、已审段落、人物未知项、候选/确认对齐、词表裁定、改后复查、译注、EPUB检查、实际阅读和交付状态。测试PASS和模型自评不是人工文学终审。若尚有未完成范围，说明实际进度和可读版本，不替用户缩减目标。

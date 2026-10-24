# 随附工具包

`scripts/ltr/` 是本 skill 的可选工具包：仅用 Python 标准库（OCR 读图另需 Pillow），按 [data-model.md](data-model.md) 建库与读写。没有工具包时，照该结构自建脚本即可，流程要求不变。

> **状态：** 接口已确定，代码正从私有实践项目迁入。下表“状态”列为“规划”的命令尚不可用；在迁入完成前不要声称已运行它们。

## 按工作流步骤

| 步骤 | 命令 | 作用 | 状态 |
| --- | --- | --- | --- |
| 1 冻结来源与词表 | `ltr import-epub <book.epub> --project <dir>` | 只读解析 EPUB，建立稳定段落 ID 与定位，保存原始快照 | 规划 |
| | `ltr import-scans <pages/> --project <dir>` | 扫描页入库（图片摘要、尺寸），不做 OCR | 规划 |
| | `ltr glossary add / confirm / lock / export` | 固定表达提案、裁定、锁定版本、导出给批次共享 | 规划 |
| 2 全书理解 | `ltr ledger add / check` | 八维理解台账，每条绑定原文定位与翻译决定；写入审校清单 | 规划 |
| 3 按证据对齐 | `ltr evidence lines --speaker <名>` / `--term <词>` / `--near <段ID>` | 查人物台词、词语在各版本的译法、段落上下文；只返回已绑定来源的结果 | 规划 |
| | `ltr align plan / run / review / index` | 对白对齐候选：有限计划、单会话串行、独立模型复核、只读索引 | 规划 |
| | `ltr attribute run / index` | 说话人/听者候选与证据片段 | 规划 |
| | `ltr quotes run / review / index` | 无引号版本的引用片段候选 | 规划 |
| | `ltr ocr vision / order / regions` | 扫描页整页 OCR、阅读顺序几何、区域转写候选 | 规划 |
| 4 生成与审校 | `ltr context build --target <段范围>` | source-first 干净上下文：原文、全书理解、已核历史证据、锁定词表；自动排除当前译稿与旧译，并记录输入清单 | 规划 |
| | `ltr candidate freeze` | 冻结新候选后才允许读取当前译稿比较 | 规划 |
| 5 改后复查 | `ltr workbench --config <cfg>` | 本机查看器：概览、对白（含全部机器意见与上下文）、扫描页、复核标签与导出 | 规划 |
| 6 交付 | `ltr assemble epub / txt` | 按最终正文组装读者版，重排译注编号与往返链接 | 规划 |
| | `python scripts/check_manifest.py <manifest.json>` | 核对申报的覆盖、改后复查与执行记录一致 | **可用** |

## 共同运行规则

所有调用模型的命令：

- 必须显式加 `--execute`；默认只做计划与预检。
- 一个进程一个会话，串行调用，历史只留在内存；思考块按原样回传以维持会话。
- 调用次数、请求字节、输出 token 三项预算是硬上限，超出前停止。
- 遇到第一个失败即停止，不自动重试；“结果未知”与“失败”分开记录。
- 每次调用前后复核来源库与图片摘要，变化即停止且不发布该批结果。
- 认证只从环境变量读取（如 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`）；模型与供应商可配置，不写入仓库。
- 输出目录必须在语料目录之外且为新建；只写候选与不含正文的摘要。

## 迁入来源

工具包从一个实际的日中轻小说项目整理而来；那里两个语料库的结构即本 skill 的标准结构。迁入时只带代码与合成测试，不带任何正文、私有路径或项目专用名单。

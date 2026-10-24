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
| 1 冻结来源 | `ltr project inspect / locate / import` | 只读解析 EPUB 元数据与导航；按显式 spine 导入，建立稳定段落定位与原始快照 |
| 3 按证据对齐 | `ltr quotes` → `quotes-review` → `quotes-index` | 无引号版本的引用片段候选、独立复核、只读索引 |
| | `ltr align` / `align-plan` / `align-campaign` → `align-review` → `align-index` | 对白对齐提议、按冻结证据规划目标、有限连续会话、独立复核、只读索引 |
| | `ltr attribute` → `attribute-index` / `attribute-projection` | 说话人/听者建议（证据片段必须是原文唯一子串）及严格投影 |
| | `ltr ocr-vision plan / run` | 整页 OCR 候选 |
| | `ltr ocr-order` | 已有 OCR 的阅读顺序几何分列 |
| | `ltr ocr-regions` | 按几何区域逐块转写，每块标记已转写或不可读，写入区域存储 |
| | `ltr publisher-scan`、`ltr review-queue` | 依已核书目查 ISBN 出现位置；生成带库指纹的待复核队列 |
| 4 生成与审校 | `ltr project check-context` | 校验上下文卡：证据来源、偏移与知情终点 |
| | `ltr project render / apply` | 生成原文-译文对照审校页；导入人工修改并保留历史版本 |
| 5 改后复查 | `ltr workbench --config <cfg> --port 0` | 本机查看器：概览、对白（上下文与全部机器意见）、扫描页叠框、复核标签与导出 |
| 6 交付 | `ltr assemble` | 按最终正文组装 TXT/EPUB |
| | `python scripts/check_manifest.py <manifest.json>` | 核对申报的覆盖、改后复查与执行记录 |

尚未实现（规划）：词表锁定命令、八维理解台账命令、跨版本证据查询命令、source-first 上下文生成与候选冻结命令。在实现前按 [evidence-and-corpus.md](evidence-and-corpus.md) 手工记录，不要声称运行过它们。

## 查看器配置

把配置文件放在书稿与仓库之外：

```json
{
  "corpora": [{
    "id": "book", "name": "某系列", "path": "../data/book-corpus",
    "candidate_index": "…/quote-index.sqlite",
    "alignment_index": "…/alignment-index.sqlite",
    "attribution_index": "…/attribution-index.sqlite",
    "ocr_vision_candidates": "…/vision-run/",
    "ocr_reading_order_candidates": "…/reading-order/",
    "ocr_regions": "…/regions.sqlite",
    "run_snapshot": "…/run-snapshot.json",
    "bibliography": "…/publisher-bibliography.json",
    "publisher_identifiers": "…/publisher-id-candidates.json",
    "source_snapshot_db": "…/dialogue.source-<sha>.sqlite", "source_snapshot_sha256": "<sha>"
  }],
  "review_dir": "…/reviews"
}
```

除 `id`、`path`（含 `dialogue.sqlite` 的目录）外都可选；每个证据路径必须在所有语料目录之外。服务只监听 `127.0.0.1`，只读打开语料库，复核标签写入 `review_dir`。

## 模型调用规则

所有调用模型的命令：

- 必须显式加 `--execute`；默认只做计划与预检。
- 一个进程一个会话，串行调用，历史只在内存；思考块按原样回传以维持会话。
- 调用次数、请求字节、输出 token 预算是硬上限，超出前停止。
- 第一个失败即停止，不自动重试；“结果未知”与“失败”分开记录。
- 每次调用前后复核来源库与图片摘要，变化即停止且不发布该批结果。
- 认证只从环境变量读取（`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`，Messages API 格式的网关均可）；模型由参数指定，不写入仓库。
- 输出目录必须新建且在语料目录之外；只写候选与不含正文的摘要。

## 其他工具

`scripts/ltr/tools/` 含归属候选准备、检测、合并、受控写库（备份、事务、回执）与语料泄露扫描（`python -m ltr.tools.leak_scan --root <数据根> --corpus <语料目录> [--declared 豁免清单.json]`）。写库工具不会被查看器调用。

部分候选 schema 名沿用最早实践项目的命名（如 `haruhi-alignment-candidates-v1`），是格式标识而非作品限定；已发布的数据按原名校验。

## 测试

```sh
python -m unittest discover -s tests -t .
```

全部使用合成数据，约一分钟。`tests/ltr/` 与 `scripts/ltr/` 包结构一一对应。

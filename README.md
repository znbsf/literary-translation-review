# Literary Translation Review

A Codex skill for source-grounded, book-length literary translation review, character dialogue evidence, cross-volume wording consistency, annotated EPUB editions, and transparent critical commentary.

这是一套可复用审校流程，不是全自动翻译软件，也不附带任何小说语料、模型账号或API服务。模型和供应商可配置；没有对特定模型的文学质量作保证。

## 使用

将本仓库复制到 Codex 的技能目录下，文件夹名为 `literary-translation-review`。在任务中使用 `$literary-translation-review`，并提供合法可用的原文、现有译稿、历史参考、期望交付范围及成本约束。

入口为 [SKILL.md](SKILL.md)。详细资料规范和读者版结构在 `references/`，可填写模板在 `assets/`。不要将私有书稿提交到此仓库。

## 无需模型的示例检查

需要 Python 3.10 或更新版本，无第三方依赖：

```sh
python scripts/check_manifest.py assets/review-manifest.example.json
python -m unittest discover -s tests -v
```

校验器检查申报的段落、注释与改后复查覆盖关系，不读取小说正文，也不证明语义、文风或设备适配正确。真实工程还需源文件绑定、实际读回和文学审校。

## 许可

本仓库代码、说明、模板和自编示例采用 MIT 许可。该许可不适用于使用者导入的作品；24小时删除提示也不是作品使用授权。

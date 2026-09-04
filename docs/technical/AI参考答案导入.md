# AI参考答案导入

AI参考答案属于独立数据域，不会写入题目及小问的原卷答案字段，也不会改变原卷答案来源状态。`final-review` 是正式 AI参考答案主表和小问表唯一的可展示内容来源；generator 和 independent 只作为相互独立的审查证据。界面仅展示最终复核为 `passed` 的记录，并固定提示“AI生成并经复核，不是原卷官方答案”。

## 数据与门禁

公共迁移新增 `ai_reference_answers` 主表和 `ai_reference_subquestion_answers` 子表。主表以 `question_id` 唯一绑定正式题目，保存题目内容哈希、答案与解析、三个模型标识、最终复核结论和备注、四份输入文件的 SHA-256 及创建时间；子表通过外键和 `display_order` 保存小问答案与解析。子表的 INSERT，以及修改 `ai_reference_answer_id`、`subquestion_id` 或 `display_order` 的 UPDATE，都会经过数据库触发器校验：小问必须属于 AI 主记录对应的同一题，且子记录顺序必须等于正式小问顺序；违反时分别以固定错误 `AI reference subquestion question mismatch` 或 `AI reference subquestion display_order mismatch` ABORT。

导入只接受同时满足以下条件的 10 题清单：

- 题目已进入 `questions` 且有 `question_sources` 正式来源绑定；
- 题目未删除，数据库中的当前 `content_hash` 与四份文件完全一致；
- `import_answer_sources.source_answer_state` 明确等于 `source_has_no_answer`；
- generator、independent、final-review 与 source 的 10 个 code/hash 逐项同序一致；
- generator、independent 和 final-review 的模型标识都必须是非空且无首尾空白的规范字符串；generator 与 independent 的模型标识必须不同，final-review 可以与 independent 使用相同模型标识；
- source、generator、independent、final-review 四份输入文件的实际字节 SHA-256 必须两两不同；
- generator、independent 以及 final-review 中 `passed` 项的小问 `display_order` 与正式题现有小问完全一致；
- final-review 的 `passed` 项必须给出严格非空的最终答案、最终解析和对应小问内容，这些内容才会写入正式 AI参考答案表；
- final-review 的 `unresolved` 和 `failed` 项必须使用空字符串答案、空字符串解析和空小问数组，只计为跳过。

所有 JSON 对象都拒绝未知、缺失和重复字段，并限制文件、文本、模型名和备注长度。导入器从四个文件的实际字节重算 SHA-256，不使用文件名推断答案来源。重复文件 SHA-256 和 generator/independent 同模型会在数据库事务开始前 fail closed。

列表、题目详情、选题篮预览和 Markdown 导出不会只依赖已保存的 `passed` 结论。每次读取时都会重新确认题目未软删除、当前题目内容哈希仍与 AI答案绑定哈希一致、正式来源关系仍存在，且来源状态仍为 `source_has_no_answer`。同时，AI 小问集合必须与该题全部正式 `subquestions` 在小问 ID、所属题目和 `display_order` 上严格一一对应；无小问题则两侧都必须为空。缺失、额外、跨题或顺序绑定漂移任一情况，都会在四个读取面整块隐藏父级答案及小问，列表也不显示“有AI参考答案”徽标。通过校验的 AI 小问从正式 `stem_markdown` 解析权威标签，因此详情、预览和 Markdown 均保留嵌套或圈号标签；缺失标签时沿用题目展示 helper 的回退语义。Markdown 导出还会对 final-review 提供的父级及小问答案/解析做 HTML 转义，以保留 Markdown/LaTeX 文本语义并阻止原生 HTML 标签被下游渲染器执行。

## JSON 契约

source 根对象字段为 `schema_version`、`questions`。每个题目项只有：

```json
{"question_code":"Q-placeholder-001","question_content_hash":"<64位小写十六进制>"}
```

generator 和 independent 根对象字段为 `schema_version`、`model`、`questions`。每个题目项为：

```json
{
  "question_code": "Q-placeholder-001",
  "question_content_hash": "<64位小写十六进制>",
  "answer_markdown": "合成答案占位文本",
  "analysis_markdown": "合成解析占位文本",
  "subquestions": [
    {"display_order": 1, "answer_markdown": "合成小问答案", "analysis_markdown": "合成小问解析"}
  ]
}
```

final-review 根对象字段为 `schema_version`、`model`、`questions`。每个题目项必须包含最终内容字段；`passed` 示例为：

```json
{
  "question_code": "Q-placeholder-001",
  "question_content_hash": "<64位小写十六进制>",
  "decision": "passed",
  "notes": "合成复核备注",
  "answer_markdown": "复核裁决后的最终答案",
  "analysis_markdown": "复核裁决后的最终解析",
  "subquestions": [
    {"display_order": 1, "answer_markdown": "最终小问答案", "analysis_markdown": "最终小问解析"}
  ]
}
```

`unresolved` 或 `failed` 项仍必须包含三个最终内容字段，但值固定为 `"answer_markdown":""`、`"analysis_markdown":""`、`"subquestions":[]`。四份文件的 `schema_version` 当前都必须是 JSON 整数 `1`，布尔值 `true` 和浮点数 `1.0` 均拒绝。`decision` 只允许 `passed`、`unresolved`、`failed`。不得把 generator 或 independent 的候选内容复制到正式表；其模型名和文件 SHA-256 会随 final-review 内容及 source 文件 SHA-256 一起参与重复导入比较，任何一项变化都会触发整批冲突并回滚。

## CLI

入口默认 dry-run，不产生 AI参考答案记录。只有显式增加 `--apply` 才写入：

```bash
.venv/bin/python scripts/import_ai_reference_answers.py \
  --database /path/to/question-bank.db \
  /path/to/source.json \
  /path/to/generator.json \
  /path/to/independent.json \
  /path/to/final-review.json
```

确认报告后，将 `--apply` 加到命令末尾。对完全相同的证据重复执行会报告 `unchanged`；同题已有不同内容或不同四文件证据时会冲突并回滚整批。

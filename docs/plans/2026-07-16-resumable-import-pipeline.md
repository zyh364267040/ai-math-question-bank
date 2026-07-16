# 可断点续跑导入导航CLI实施计划

> **For Hermes:** 使用Codex按TDD实现，Hermes负责真实任务幂等验证和独立复审。

## 目标

新增一个统一CLI，从数据库和现有工件实时推导导入任务处于哪一步，避免每次重新检查整个项目。第一阶段只自动恢复已经具备完整锁、崩溃恢复和原子发布能力的页面渲染；视觉识别、裁图、审核和入库继续使用现有外部或严格服务。

## 最终范围

1. 新命令：`python3 -m src.pipeline.import_pipeline --job-id N`。
2. 默认严格只读，不修改数据库或文件。
3. 不创建额外的 `pipeline_state.json`。
4. 输出稳定的 `stage`、`next_action`、`changed` 和消息。
5. `--apply` 仅在 `next_action=render_pages` 时调用现有页面渲染服务。
6. 外部 `question_regions.json` 不由新CLI自动执行。
7. 工件齐备后返回 `run_strict_admission`，但不在新CLI中重复实现评估、备份、入库和状态收口。
8. `completed` 只信任数据库任务状态；不得根据可变候选文件修复历史状态。
9. `needs_review` 保持人工复核，不回退重复渲染。
10. 不调用Codex、OCR或网络，不清理私人数据。

## 实现文件

- 新建 `src/pipeline/__init__.py`
- 新建 `src/pipeline/import_pipeline.py`
- 新建 `tests/test_import_pipeline.py`
- 更新 `README.md`

## 安全与精简要求

1. SQLite检查使用 `mode=ro`。
2. 工件读取有大小上限，通过目录文件描述符读取并拒绝符号链接。
3. 缺失、损坏、跨任务工件必须失败关闭。
4. CLI参数错误和运行异常返回稳定结果；JSON模式始终输出JSON。
5. 新CLI不得复制裁图、入库、备份、状态事务或新的锁实现。
6. 页面渲染直接复用 `claim_render_job` 和 `run_claimed_render`。
7. 自动步骤失败时不得泄露内部异常信息。

## TDD验收

1. 从无页面到候选、裁图、裁图审核、AI审核、严格入库提示和完成状态均可稳定推导。
2. 默认检查数据库和文件散列不变。
3. 缺失数据库不会创建空库。
4. 跨任务和损坏工件失败关闭。
5. 符号链接任务目录被拒绝。
6. 旧 `needs_review` 任务不会重新渲染。
7. 已有正式来源但状态陈旧时不会根据候选文件自动修复。
8. `--apply` 只调用现有页面渲染服务，绝不调用裁图、备份或入库。
9. 页面渲染异常返回 `failed` 且 `changed=false`。
10. 目标测试、全量测试、Ruff、compileall、JavaScript语法和Git差异检查全部通过。
11. 用正式任务1和4做只读/幂等验证，数据库题量及完整性不变。
12. 独立复审P0、P1、P2全部为空后才能提交。

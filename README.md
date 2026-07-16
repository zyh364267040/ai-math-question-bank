# AI 数学题库

面向个人数学教学的本地题库工具，也是一个 AI 产品经理实践项目。当前优先支持天津高中数学试卷，目标是把“试卷安全接收、外部 AI/OCR 结果审核、正式入库、选题预览、Markdown 导出”连接成可复用流程。

## 当前能力

- 通过 Web 文件选择器接收用户主动上传的 PDF，先展示文件名、大小、页数和哈希，只有再次确认后才创建导入任务
- PDF 预览阶段只写入被 Git 忽略的私有暂存区，不扫描桌面、下载目录或其他本地文件，也不启动 OCR 或 AI
- 上传入口限制请求体大小；暂存清单使用持久 HMAC 签名，确认操作使用数据库幂等收据和文件锁，避免篡改及并发重复建任务
- 用户确认导入后仍需再次点击“开始页面处理”；系统才会按确认页码以 300 DPI 在私有目录中生成经过尺寸、哈希和格式校验的页面 PNG
- 页面渲染支持进度查看、并发防重、失败重试、崩溃恢复和原子发布；全局只运行一个渲染任务，并限制整批像素、输出大小和最低磁盘余量，不会自动启动 OCR、切题或 AI
- 页面渲染完成后，用户还需显式点击“开始版面分析”；系统才会在私有目录中判断单栏、双栏或三栏版式，并结合 PDF 自带文本层生成主题号及跨栏、跨页题目边界候选
- 版面分析会生成带边框的逐页预览和可校验清单；没有文本层时只报告栏结构并明确交给人工复核，不启动 OCR、不猜题号、不自动裁题，也不修改正式题库
- 页面渲染完成后，用户可显式点击“调用 Codex 自动切题”；系统只发送该任务经数据库摘要锚定的页面快照，禁用 Codex shell 工具，严格校验结构化边界后生成签名单题 PNG 和联系表
- 自动切题支持跨页、多区域、并发防重、失败保留上一版结果和崩溃恢复；新裁图始终保持 `pending_ai_review`，不会自动审核、入库或生成题干 OCR
- 按外部提供的裁切计划生成完整题图和必要配图
- 读取外部 AI/OCR 流程生成的 `candidate_questions.json` 和 `ai_audit.json`
- 依据严格 AI 二审信号筛选可自动入库题目，其余保留在候选审核区
- 原题与识别结果并排核对，支持题干、选项和小问原位修改
- 保留“公共条件＋子问”的真实层级，如（2）下的（i）（ii）或①②
- 审核来源区分为“人工审核通过”和“AI二审通过”
- 审核通过后安全同步正式题库，并记录版本和审计信息
- 正式题库完整展示题干、全部选项、所有小问和解题必需图片
- 支持筛选、选题篮、实时预览和Markdown导出
- 正式题与候选题均采用可恢复的软删除

确认导入只会安全归档原卷并建立 `pending` 任务；只有用户再次明确点击后才渲染页面。自动切题也必须由用户在对应任务页面单独授权，且只生成待审核题图。当前仓库仍不自动执行题干 OCR、公式识别、候选题生成或 AI 二审；这些结果必须由外部工具产生后再交给本仓库校验、审核和入库。

## 统一导入流水线（第一阶段）

统一 CLI 从数据库、渲染记录和现有工件实时派生状态，不创建额外的 `pipeline_state` 文件。默认命令只检查，不修改数据库或文件：

`python3 -m src.pipeline.import_pipeline --job-id 1`

需要显式指定测试副本或其他位置时，可同时传入 `--database /path/to/question-bank.db --private-root /path/to/private-root`。机器可读输出使用 `--json`。

确认恢复页面渲染时添加 `--apply`：

`python3 -m src.pipeline.import_pipeline --job-id 1 --apply`

`--apply` 第一阶段只会复用现有具备锁、恢复和原子发布能力的页面渲染服务。候选识别、区域裁图、视觉复核和严格入库只返回明确的 `next_action`，不会在新CLI中重复实现这些安全边界。CLI不会调用 Codex、OCR 或网络，不会创建 `pipeline_state`，也不会根据可变候选文件修复历史任务状态。

稳定的 `next_action` 含义如下：

- `check_job_id`：任务不存在，请核对任务号。
- `check_database`：数据库路径不存在、不可读或结构不完整；检查不会创建空数据库。
- `render_pages`：可用 `--apply` 执行确定性页面渲染。
- `wait_or_recover`：任务已处于处理中，不重复启动；等待完成或通过原服务恢复。
- `provide_candidate_questions`：需要外部视觉流程提供候选题。
- `provide_crop_plan`：外部视觉流程负责区域计划和裁图，新CLI不会直接写裁图。
- `review_crops`：裁图仍需视觉审核，不会自动通过。
- `provide_ai_audit`：需要外部流程提供 AI 审核清单。
- `run_strict_admission`：视觉工件齐备，应调用现有严格入库服务；新CLI不重复执行备份和入库。
- `manual_review`：存在待人工或不安全题目，任务保持 `needs_review`。
- `none`：数据库任务状态已经是 `completed`，无需执行。

## 审核规则

系统使用三级审核结果：

- AI自动通过：第二遍审核为高置信度，且没有问题或修改建议
- AI有争议：存在可定位的问题，需要修正
- 需要人工确认：原图不清、结构不确定或关键公式无法确认

用户只复核重点或异常题。AI二审无问题的题可以直接通过，不要求逐题人工审核。AI自动通过只代表题干、结构、公式和配图复核通过，不代表缺失答案已经被补全。原卷没有答案时，系统保持“原卷未提供答案”，不会让AI猜测。

## 本地启动

1. 使用 macOS 或 Linux（安全文件锁、`dir_fd` 和 `O_NOFOLLOW` 依赖 POSIX），安装 Python 3.12 或兼容版本。
2. 在项目根目录运行 `python3 -m pip install -r requirements.txt`；开发和测试环境使用 `requirements-dev.txt`。
3. 运行 `python3 scripts/start_web.py`。
4. 浏览器访问 <http://127.0.0.1:8000>。

服务只监听本机地址，题库数据不会自动上传。

## 测试与检查

运行全部测试：`python3 -m unittest discover -s tests -q`

检查Python语法：`python3 -m compileall -q src tests scripts`

检查前端脚本：`for f in src/web/static/*.js; do node --check "$f"; done`

检查 Python 正确性规则：`ruff check .`

## 审核批次收口

默认以只读方式预览：

`python3 scripts/finalize_review.py --job-id 1 --database data/private/question-bank.db --private-root data/private`

确认结果后正式执行：

`python3 scripts/finalize_review.py --job-id 1 --database data/private/question-bank.db --private-root data/private --apply`

正式执行前会自动备份SQLite数据库。流程具有事务保护、版本检查和幂等性，重复执行不会重复修改题目。

## 数据安全

以下内容不会进入Git：

- 原始PDF和扫描图片
- SQLite数据库及备份
- OCR中间结果和审核证据
- 导出文件、日志和PID文件
- `.env`及本地开发环境文件

仓库只保留代码、测试、产品文档、数据库结构和非私有示例数据。

## 项目结构

- `src/importing/`：PDF 安全接收、外部候选结果校验和正式入库
- `src/processing/`：执行外部裁切计划，生成单题图和必要配图
- `src/reviewing/`：审核批次收口与正式题库同步
- `src/pipeline/`：从现有数据库和工件派生状态，幂等推进确定性导入步骤
- `src/database/`：SQLite结构、迁移和选题篮存储
- `src/web/`：FastAPI本地Web界面
- `scripts/`：启动和维护命令
- `tests/`：自动化测试
- `docs/`：产品、领域和技术文档
- `data/private/`：本地私有数据，仅保留占位文件

## 当前阶段

三份真实试卷已经在本地私有数据中完成闭环验证，累计 62 道正式题（这些题目和原卷不会上传到 Git）。当前已实现：“用户主动选择 PDF → 预览并确认 → 创建任务 → 页面渲染 → 可选版面分析 → 接收外部视觉候选和区域计划 → 确定性裁图 → AI二审门禁 → 备份并正式入库 → 任务状态收口”。统一流水线负责检查和推进已有确定性步骤；AI/OCR识别、裁图视觉复核和候选生成仍由仓库外工具完成。

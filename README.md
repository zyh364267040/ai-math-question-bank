# AI 数学题库

面向个人数学教学的本地题库工具，也是一个 AI 产品经理实践项目。当前优先支持天津高中数学试卷，目标是把“试卷安全接收、外部 AI/OCR 结果审核、正式入库、选题预览、Markdown 导出”连接成可复用流程。

## 当前能力

- 通过 Web 文件选择器接收用户主动上传的 PDF，先展示文件名、大小、页数和哈希，只有再次确认后才创建导入任务
- PDF 预览阶段只写入被 Git 忽略的私有暂存区，不扫描桌面、下载目录或其他本地文件，也不启动 OCR 或 AI
- 上传入口限制请求体大小；暂存清单使用持久 HMAC 签名，确认操作使用数据库幂等收据和文件锁，避免篡改及并发重复建任务
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

确认导入只会安全归档原卷并建立 `pending` 任务。当前仓库不负责 PDF 页面渲染、OCR、公式识别、调用 AI，也不会自行生成候选题或 AI 审核文件；这些结果必须由外部工具产生后再交给本仓库校验、审核和入库。

## 审核规则

系统使用三级审核结果：

- AI自动通过：第二遍审核为高置信度，且没有问题或修改建议
- AI有争议：存在可定位的问题，需要修正
- 需要人工确认：原图不清、结构不确定或关键公式无法确认

用户只复核重点或异常题。AI二审无问题的题可以直接通过，不要求逐题人工审核。AI自动通过只代表题干、结构、公式和配图复核通过，不代表缺失答案已经被补全。原卷没有答案时，系统保持“原卷未提供答案”，不会让AI猜测。

## 本地启动

1. 安装 Python 3.12 或兼容版本。
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
- `src/database/`：SQLite结构、迁移和选题篮存储
- `src/web/`：FastAPI本地Web界面
- `scripts/`：启动和维护命令
- `tests/`：自动化测试
- `docs/`：产品、领域和技术文档
- `data/private/`：本地私有数据，仅保留占位文件

## 当前阶段

两份真实试卷已经在本地私有数据中完成闭环验证，累计 42 道正式题（这些题目和原卷不会上传到 Git）。当前阶段开始建设 `v0.2.0`：第一步已经实现“用户主动选择 PDF → 预览文件信息 → 明确确认 → 创建待处理任务”；页面渲染、AI/OCR 识别和候选生成仍由仓库外工具完成。

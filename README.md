# KnowFlow V1 Core

KnowFlow 是一个中文知识工作台，当前交付范围为 V1 Core：员工、管理员和游客登录；连续对话；会话 PDF 附件问答与引用预览；知识投稿、审核、发布和知识范围控制；资源永久删除、外部清理和管理员重试。

本 README 面向当前已经配置好的工作区，用于启动和演示现阶段成果。它不是一份全新环境安装指南，也不新增产品 contract。

## 当前环境

启动前确认以下服务和配置已经可用：

- 根目录 `.env`：运行时数据库、模型、MinIO、Elasticsearch 和 Docling 离线缓存配置；不要把凭据提交到 Git。
- MySQL：Compose 服务 `knowflow-mysql-1`，或 `.env` 中 `KNOWFLOW_MYSQL_URL` 指向的实例。
- MinIO：容器 `knowflow-minio-1`，正式 bucket 使用 `.env` 中的 `KNOWFLOW_MINIO_BUCKET`。
- Elasticsearch：容器 `knowflow-elasticsearch-1`，正式 index 使用 `.env` 中的 `KNOWFLOW_ELASTICSEARCH_INDEX`。
- 本机 SSH 隧道：将远端 MinIO/Elasticsearch 映射到 `.env` 的 `127.0.0.1:19000` 和 `127.0.0.1:19200`。
- 本地 Python、Node/npm、`apps/api/.venv` 和 `apps/web/node_modules` 已存在。

容器名是运维标识，不是数据命名空间；不要把容器名写入 `KNOWFLOW_MINIO_BUCKET` 或 `KNOWFLOW_ELASTICSEARCH_INDEX`。正式数据仍使用当前 `.env` 指定的 `knowflow-t015` bucket/index。

## 启动

### Windows 一键启动 / 停止

在根目录双击 **`start.bat`**。脚本检查 `.env`、已有依赖和端口，按当前配置补建缺少的 SSH 隧道，依次等待 API 和 Web 就绪，然后自动打开 <http://127.0.0.1:5173/>。SSH 如需密码或首次确认主机指纹，请在弹出的 SSH 窗口中完成。

保留启动窗口，在该窗口**按回车或 `Ctrl+C` 停止**本次启动的前后端和隧道。已有隧道会保留；8000 或 5173 已被占用时会报错，避免重复启动。停止会结束进程树，进行中的请求会中断；请等待重要处理完成后再停止。

停止时窗口会依次显示 `Stopping KnowFlow...` 和 `KnowFlow stopped.`。更新启动脚本前已打开的窗口仍运行旧逻辑，可先在旧窗口按回车停止，再重新双击 `start.bat`。Ctrl+C 需要在启动窗口中按下。

日志写入 `.run/api.log`、`.run/api.error.log`、`.run/web.log` 和 `.run/web.error.log`，每次启动覆盖，目录已加入 Git 忽略。脚本面向上文已配置好的工作区，不安装依赖、不执行数据库迁移、不启动或停止远端容器。

可选命令（PowerShell）：

```powershell
.\start.bat -Check                         # 仅检查环境，不启动服务
.\start.bat -NoBrowser                     # 启动但不自动打开浏览器
.\start.bat -SshTarget user@your-server     # 覆盖 README 默认 SSH 目标
```

若缺少依赖，先安装 Python 3.12、uv、Node.js 24 / npm 11，再运行 `uv sync --project apps/api` 和 `npm --prefix apps/web ci`；`.env` 仍需按实际环境配置。

### 手动启动

在 PowerShell 中打开三个终端。若远端资源尚未通过隧道提供，先运行：

```powershell
ssh -N -L 19000:127.0.0.1:19000 -L 19200:127.0.0.1:19200 knowflowadmin@127.0.0.1
```

终端一启动 API 单 worker：

```powershell
uv run --project apps/api --env-file .env uvicorn knowflow.app.main:create_app --factory --host 127.0.0.1 --port 8000
```

终端二启动 Web：

```powershell
npm --prefix apps/web run dev -- --host 127.0.0.1 --port 5173
```

打开 <http://127.0.0.1:5173/>。API 由 Web 的 `/api` 代理转发到 `http://127.0.0.1:8000`。

停止时分别在对应终端按 `Ctrl+C`；SSH 隧道也按 `Ctrl+C` 关闭。V1 Core 固定使用一个 API worker，不能使用 `--workers` 启动多个进程。

## 演示路径

使用 `.env` 中的预置员工账号或管理员账号登录：

1. 员工登录，创建会话并重命名，发送普通问题，确认回答完成。
2. 打开知识范围，查看可用知识源；关闭知识库后发送明确的 PDF 问题，确认系统给出“当前没有可用资料”的确定性拒答，而不是用通用知识补答。
3. 使用已有真实 PDF 演示附件处理和引用预览；附件处理阶段会显示真实状态，引用只允许查看回答实际引用的页。
4. 员工删除会话，确认不可恢复提示，永久删除后回到“选择一个会话”。
5. 管理员重新登录，打开“Knowledge 管理”，查看待审核、知识库、历史记录和清理失败视图；按已有投稿流程执行审核、发布、启停或清理失败重试。

T040 的完整 UAT、真实 PDF smoke/formal-dev 结果和限制见 [`docs/knowledge/uat-core.md`](docs/knowledge/uat-core.md)；删除专项见 [`docs/knowledge/uat-deletion.md`](docs/knowledge/uat-deletion.md)。

## 评估入口

评估使用 `evals/knowledge/` 中 T026 冻结的真实 PDF、问题集和结果目录。数值指标用于性能参考和版本比较，不是功能硬门禁；结果中的 `gate_eligible` 只表示样本完整性。

```powershell
uv run --project apps/api --env-file .env python evals/knowledge/run_eval.py test-fast
uv run --project apps/api --env-file .env python evals/knowledge/run_eval.py test-real-ai --kind smoke
uv run --project apps/api --env-file .env python evals/knowledge/run_eval.py evaluate-rag --kind formal
uv run --project apps/api --env-file .env python evals/knowledge/run_eval.py evaluate-rag-holdout --kind formal
```

`test-fast` 是确定性回归和 Web Gate；`test-real-ai` 验证真实模型和基础设施链路；`evaluate-rag` 与 `evaluate-rag-holdout` 分别对应 dev 与 holdout。当前 holdout 因缺少合法冻结 redacted input 未执行，不应把 dev 报告复制为 holdout。

## 架构边界

浏览器通过 FastAPI 访问 MySQL、私有 MinIO bucket、Elasticsearch chunk index，以及独立的 Chat、Embedding、Rerank adapter。附件使用持久队列，Docling 负责 PDF 解析，应用控制检索来源、轮次、证据和预算；引用由实际采用的页级证据生成。

当前检索回答采用 `core-exp-2-extractive` 原文摘录策略：识别本轮允许文档的标题／文件名，模型选择相关片段，后端验证后返回实际原文和页码，不自由改写。明确命名单份文档的单一问题不会因标题被扩大为多个问题；日常通用对话保持可用。已启动的 API 需重启后生效，历史回答不变。

点击回答引用可在右侧预览对应 PDF 原文。右上角按钮可折叠／展开资料栏，拖动栏左侧分隔线可调整宽度，双击恢复默认；分隔线也支持左右方向键微调、Home／End 调到最小／最大。浏览器会保存布局偏好，点击引用会自动展开资料栏，PDF 随栏宽缩放。

Core 包含 US1～US7 的既有能力和四类资源删除闭环，不包含账号管理/删除、System 或 Run 诊断、开放工具循环、Web Search、代码执行、多 Agent、后台自动重试调度、独立 Worker 或普通用户完整 PDF 下载。

## 已知限制

- Docling 必须使用 `.env` 中的本地 Hugging Face 缓存和 `HF_HUB_OFFLINE=1`；缺少离线配置可能导致 tokenizer 元数据请求超时，系统按既有 contract 返回 `ATTACHMENT_PROCESSING_FAILED`。
- 真实模型指标会波动，指标低于参考线不自动否定功能可用性。
- formal holdout 需要合法 redacted input；当前未执行。
- HTTP slow 回归剩余失败属于远端 MySQL/Docling 时序限制，已在 T040 记录，不代表 MinIO 凭据或 PDF 页码问题。
- V1 Core 以单管理员、单 API worker 为前提；管理员发布需等待当前发布完成，不同投稿的瞬时并发批准不属于 Core 验收。

T040 已完成，T041 完成后 V1 Core Gate 立即停止。未来能力必须重新通过 Spec/Plan/Tasks Gate，不从 README 直接扩展。

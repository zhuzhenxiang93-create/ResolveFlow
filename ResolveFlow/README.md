# ResolveFlow 完整使用指南

新增 Agent 业务执行入口：`make agent-demo` 可离线演示工具调用、多步执行、
跨轮任务状态、人工审批恢复、主辅证据交接及业务结果核验。
接口与模型配置见 [Agent 执行指南](wiki/agent-execution.md)。

本轮业务执行增强提供 `make agent-eval`（内部确定性回归）和 `make agent-eval-live`
（显式真实模型验证，缺少凭据/预算时明确降为离线）。报告独立保存，不覆盖 RAG 指标。
查询不自动授权修改；退款先绑定审批，再根据模拟数据库核验结果。
调研与技术取舍见 [外部调研](wiki/external_research.md)、[采用决策](wiki/adoption_decisions.md)。

2026-09-11：业务 action 新增 [LLM 目标理解与用户确认](wiki/goal-understanding-confirmation.md)。
任何实际修改先进入 `awaiting_confirmation`，用户通过确认接口同意具体操作；退款另需人工审批。
业务数据均为模拟。

2026-09-12：`/chat` 收敛为唯一对话入口（旧 mode=chat/action 直接调用编排器的入口已下线），
鉴权扩展到 `/search`、`/knowledge/*`、`/skills`、`/monitor`、`/eval/run`（分层：只读任意角色，
写/运维需 `role=admin`），身份统一为本地签发 JWT（`user`/`reviewer`/`admin` 三角色），
见 [2.4 获取身份令牌](#24-获取身份令牌新增2026-09-12-起必需)。`ResolveFlowFrontend`
（独立 Vue 项目）已接入这条统一链路，含身份令牌管理、模拟订单、任务卡、确认/审批交互；
[Postgres 迁移方案与 POC](wiki/postgres-migration-plan.md) 已产出但生产 Action 层仍是 SQLite，
未做真实切换。旧任务 schema 不复用旧授权，请新建任务。

本文档说明 ResolveFlow 的部署、启动、API 调用、知识库使用、ChromaDB 数据查看、监控评测和常见排障。

ResolveFlow 是一个企业级智能客服系统，核心链路为：

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取 Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像
  -> IntentRecognizer 识别意图
  -> AgentOrchestrator 选择主 Agent 与辅助 Agent
  -> 复合请求生成结构化 TaskPlan，按 DAG 串并行调度
  -> ResponseSynthesizer 去重、处理部分失败与明显冲突
  -> Safety Guard / Human-in-the-loop
  -> LLM 生成回复
  -> 写入 Redis，并异步更新 ChromaDB 用户画像
```

## 1. 项目结构

```text
ResolveFlow/
├── api/main.py                    # FastAPI 入口，/chat /search /knowledge /monitor /eval
├── core/intent_recognizer.py      # 三路融合意图识别
├── agents/agent_orchestrator.py   # 多 Agent 路由编排
├── agents/supervisor.py           # TaskPlanner、DAG Scheduler、受约束结果合成
├── memory/conversation_memory.py  # Redis + ChromaDB 记忆管理
├── mcp/tool_manager.py            # MCP 工具调用、查询改写、重排、熔断、缓存、降级
├── mcp/knowledge_base.py          # ChromaDB + BM25 双路召回与 RRF 融合
├── mcp/hybrid_retriever.py        # 中英混合分词、增量 BM25、加权 RRF
├── mcp/qwen_reranker.py           # Qwen3 专用精排客户端与响应校验
├── monitor/performance_monitor.py # Agent/工具在线监控
├── evaluation/evaluator.py        # 端到端评测
├── data/demo_docs/                # 演示知识库文档
├── docker-compose.yml             # Docker 全栈编排
├── Dockerfile
├── requirements.txt
└── .env
```

## 2. 环境准备

### 2.1 必需依赖

- Docker
- Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的第三方 API Key

### 2.2 配置 `.env`

复制示例文件：

```bash
cp .env.example .env
```

最少需要配置：

```env
ANTHROPIC_API_KEY=your_api_key
```

如果使用 DeepSeek 这类 Anthropic 兼容接口，可以配置：

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_API_KEY=your_deepseek_key
```

Docker Compose 场景下，Redis 和 ChromaDB 的连接由 `docker-compose.yml` 覆盖为容器内地址。通常不需要手动改：

```env
REDIS_PASSWORD=resolveflow123
CHROMA_HOST=localhost
CHROMA_PORT=8001
```

### 2.3 全栈部署和 run 开发模式的区别

ResolveFlow 常用两种 Docker 启动方式：`docker compose up` 全栈部署，以及 `docker run` 开发模式。两者最大的区别是：**全栈部署会同时启动应用和依赖服务；run 开发模式通常只手动运行一个应用容器，依赖服务需要提前启动**。

| 对比项 | Docker Compose 全栈部署 | Docker run 开发模式 |
|--------|--------------------------|----------------------|
| 启动命令 | `docker compose up -d --build` | `docker run ... resolveflow ...` |
| 启动内容 | ResolveFlow、Redis、ChromaDB、Prometheus、Nginx | 只启动你指定的单个容器 |
| Redis/ChromaDB | 自动启动并加入同一网络 | 必须先执行 `docker compose up -d redis chromadb` |
| 容器网络 | Compose 自动创建并管理 | 需要手动指定 `--network resolveflow_resolveflow-network` |
| 服务名解析 | 应用可直接访问 `redis`、`chromadb` | 只有加入同一网络后才可访问 `redis`、`chromadb` |
| 代码更新 | 通常需要 rebuild 或重启服务 | 挂载 `-v "$(pwd):/workspace"` 后，代码修改可直接生效，重启容器即可 |
| 适合场景 | 演示、联调、完整部署、HTTP API 服务 | 本地开发、调试 CLI、临时覆盖环境变量 |
| 常见问题 | API Key 或依赖健康检查失败 | 忘记启动 Redis/ChromaDB，导致 `redis:6379 Name or service not known` |

选择建议：

- 想完整体验 HTTP API、Swagger、Nginx、Prometheus：用 **Docker Compose 全栈部署**。
- 想调试源码或 CLI，并且希望本地改代码后快速重跑：用 **Docker run 开发模式**。
- 如果只是跑 CLI，最省心的方式是 `docker compose run --rm resolveflow python api/main.py --cli`，它会自动使用 Compose 网络。

### 2.4 获取身份令牌（新增，2026-09-12 起必需）

`/chat` 和其余所有业务接口（除 `/health`、`/metrics`）现在都要求本地签发的 JWT，不再信任
匿名请求。先在 `.env` 里配置好签名密钥和管理员引导密钥：

```env
AGENT_JWT_SECRET=一串随机字符串
AGENT_ADMIN_SECRET=另一串随机字符串
```

服务起来之后，用管理员密钥签发一个 `role=user` 的 token（其余 curl 示例都会引用 `$TOKEN`）：

```bash
export TOKEN=$(curl -s -X POST http://localhost:8000/agent/auth/token \
  -H "X-Admin-Secret: your_admin_secret" -H "Content-Type: application/json" \
  -d '{"subject":"demo","role":"user","ttl_seconds":3600}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
```

知识库写入、`/monitor`、`/skills/reload`、`/eval/run` 这类运维/写操作需要 `role=admin`：

```bash
export ADMIN_TOKEN=$(curl -s -X POST http://localhost:8000/agent/auth/token \
  -H "X-Admin-Secret: your_admin_secret" -H "Content-Type: application/json" \
  -d '{"subject":"ops","role":"admin","ttl_seconds":3600}' | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
```

本地开发（不经过 HTTP）可以直接用 `make mint-token SUBJECT=demo ROLE=user` 签发，见
[Agent 执行指南](wiki/agent-execution.md)。角色边界、审批自批拒绝等细节也在那篇文档里。

## 3. Docker Compose 全栈部署

推荐使用此方式启动完整服务。

```bash
docker compose up -d --build
```

查看服务状态：

```bash
docker compose ps
```

查看应用日志：

```bash
docker compose logs -f resolveflow
```

看到 ResolveFlow 启动日志并且健康检查通过后，服务可用。

启动后的端口：

| 服务 | 容器名 | 宿主机端口 | 容器内端口 | 用途 |
|------|--------|------------|------------|------|
| ResolveFlow API | `resolveflow-app` | `8000` | `8000` | 主 API 服务 |
| Nginx | `resolveflow-nginx` | `80` | `80` | 反向代理 |
| ChromaDB | `resolveflow-chromadb` | `8001` | `8000` | 向量数据库 |
| Redis | `resolveflow-redis` | `6379` | `6379` | 工作记忆 |
| Prometheus | `resolveflow-prometheus` | `9090` | `9090` | 监控数据 |

健康检查：

```bash
curl http://localhost:8000/health
```

Swagger 文档：

```text
http://localhost:8000/docs
```

也可以通过 Nginx 访问：

```bash
curl http://localhost/health
```

## 4. Docker Run 开发模式

开发时可以只用 Compose 启动依赖，然后用 `docker run` 挂载当前代码目录。

先启动 Redis 和 ChromaDB：

```bash
docker compose up -d redis chromadb
```

构建镜像：

```bash
docker compose build --no-cache resolveflow
```

启动 HTTP 服务：

```bash
docker run -it --rm \
  --network resolveflow_resolveflow-network \
  -p 8000:8000 \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:resolveflow123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -e CHROMA_PERSIST_DIRECTORY="/workspace/data/chroma" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  resolveflow
```

CLI 交互模式：

```bash
docker run -it --rm \
  --network resolveflow_resolveflow-network \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:resolveflow123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  resolveflow \
  python api/main.py --cli
```

## 5. Swagger 和接口总览

ResolveFlow 基于 FastAPI 构建，启动 HTTP 服务后可以直接在浏览器访问 Swagger UI 调用接口。

本地 Swagger 地址：

```text
http://localhost:8000/docs
```

如果使用 Nginx 反向代理：

```text
http://localhost/docs
```

打开 Swagger 后，可以点击任意接口右侧的 **Try it out**，填写参数后点 **Execute** 直接调用本地服务。
除 `/health`、`/metrics` 外都要求 `Authorization` 请求头（见 [2.4 获取身份令牌](#24-获取身份令牌新增2026-09-12-起必需)）；
Swagger 这里把它显示成普通请求头参数、不是右上角一次性 Authorize 锁图标，需要在每个接口的
`authorization` 参数里手动填 `Bearer <token>`。常用调试顺序：

```text
1. GET /health                确认服务是否就绪
2. POST /chat                 测试主对话链路（需要 role=user 的 Bearer token）
3. GET /knowledge/stats       查看知识库是否已有数据（需要任意角色的 Bearer token）
4. POST /knowledge/upload     上传演示知识库文件（需要 role=admin 的 Bearer token）
5. POST /search               测试知识库检索、查询改写和重排（需要任意角色的 Bearer token）
6. GET /monitor               查看 Agent 和工具运行指标（需要 role=admin 的 Bearer token）
7. GET /skills                查看已加载 Skills（需要任意角色的 Bearer token）
8. POST /skills/reload        重新加载 Skills（需要 role=admin 的 Bearer token）
9. POST /eval/run             运行端到端评测（需要 role=admin 的 Bearer token）
```

## 6. 可复现评测与简历指标

项目提供 Makefile 统一入口，既可以从仓库根目录执行，也可以进入 `ResolveFlow/` 后执行。

```bash
make test            # 单元测试与数据静态校验
make routing-eval    # holdout 上的离线路由、协作与升级规则评测
make metrics-report  # 汇总已有 eval/RAG 报告，生成 latest_metrics.json/md
make rag-eval        # 隔离 ChromaDB 运行混合检索 Top-K/MRR 验收
make eval-smoke      # 无 API Key 可跑：test + routing-eval + metrics-report
make eval-final      # test + routing-eval + rag-eval + metrics-report
```

输出文件：

```text
data/eval/reports/latest_rag.json
data/eval/reports/latest_routing.json
data/eval/reports/latest_metrics.json
data/eval/reports/latest_metrics.md
```

`latest_metrics.md` 中的 `Resume-safe bullets` 只使用内部 golden set 和真实报告里的数值，适合复制到简历前复核。未接入真实线上流量前，不应写线上用户数、线上 QPS 或生产 SLA。

### 5.1 接口总览

| 方法 | 路径 | 参数位置 | 作用 | 适合场景 |
|------|------|----------|------|----------|
| `GET` | `/health` | 无 | 健康检查，返回服务状态和 Agent 统计 | 启动后确认服务可用 |
| `POST` | `/chat` | JSON Body | 主对话接口，完成记忆读取、意图识别、Agent 路由、回复生成、记忆写入 | 业务主链路 |
| `GET` | `/monitor` | 无 | 查看 Agent/工具统计、告警和优化建议 | 观察在线表现 |
| `POST` | `/search` | Query 参数 | 查询改写、向量/BM25 独立召回、RRF 融合、合并去重、Qwen3 重排 | 测试 RAG 检索及降级状态 |
| `GET` | `/skills` | 无 | 查看当前加载的 Skills、匹配关键词和解析错误 | 确认动态能力是否生效 |
| `POST` | `/skills/reload` | 无 | 运行时重新扫描 Skill 目录 | 修改业务规则后热加载 |
| `POST` | `/knowledge/add` | JSON Body | 批量导入文档到 ChromaDB 知识库 | 程序化导入文档 |
| `POST` | `/knowledge/upload` | Form File | 上传 `.txt`、`.md`、`.json` 文件导入知识库 | 手动上传知识库文件 |
| `GET` | `/knowledge/stats` | 无 | 查看知识库文档片段总数 | 确认知识库是否有数据 |
| `POST` | `/eval/run` | 无 | 运行内置意图识别和端到端对话评测 | 演示 LLM-as-Judge 评测 |
| `GET` | `/docs` | 浏览器访问 | Swagger UI | 浏览和调试所有接口 |

### 5.2 Skills 动态能力加载

ResolveFlow 支持从目录加载 Skills，用来把业务流程、客服话术、排障 SOP 等规则动态注入 Agent。

默认配置：

```env
RESOLVEFLOW_SKILLS_DIR=./skills
RESOLVEFLOW_SKILLS_MAX_PROMPT_CHARS=5000
```

推荐结构：

```text
skills/refund/SKILL.md
skills/customer_support/SKILL.md
```

`SKILL.md` 示例：

```markdown
---
name: 退款处理流程
description: 退款场景的客服处理规则
keywords: 退款,退费,refund
agents: billing,general
enabled: true
---

# 退款处理流程

- 先确认订单号和支付方式。
- 涉及实际退款操作时转人工审核。
```

查看加载结果：

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/skills
```

修改 Skill 文件后热加载：

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/skills/reload
```

### 5.3 `/health`

用途：确认服务是否初始化完成。

```bash
curl http://localhost:8000/health
```

响应示例：

```json
{
  "status": "ok",
  "agents": {
    "general_0": {
      "total": 0,
      "success_rate": 1.0,
      "avg_ms": 0.0,
      "monitor_penalty": 0.0,
      "routing_score": 1.0
    }
  }
}
```

### 5.4 `/chat`

用途：主对话接口。

请求体：

```json
{
  "message": "我要退款",
  "user_id": "user_001",
  "conv_id": "session_001"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `message` | 是 | 用户输入 |
| `user_id` | 否 | 用户 ID，默认 `anonymous` |
| `conv_id` | 否 | 会话 ID，不传则自动生成 |

返回字段：

| 字段 | 说明 |
|------|------|
| `conv_id` | 会话 ID |
| `response` | Agent 回复 |
| `intent` | 意图识别结果 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级 |
| `latency_ms` | 端到端耗时 |
| `task_plan` | 复合请求的脱敏结构化子任务；单 Agent 快速路径为 `null` |
| `execution_trace` | 子任务状态、依赖、Agent 和延迟，不包含回答正文 |
| `synthesis_method` | `single_agent`、`deterministic` 或 `fallback` |
| `degradations` | Planner、Scheduler、Synthesizer 等降级标记 |

### 5.5 `/search`

用途：测试 MCP 工具调用和 RAG 检索优化。

Query 参数：

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | 是 | 无 | 用户检索问题 |
| `top_k` | 否 | `5` | 返回结果数量 |

示例：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/search?query=退款多久到账&top_k=3"
```

### 5.6 `/knowledge/add`

用途：通过 JSON 批量导入知识库。

请求体：

```json
{
  "documents": [
    {
      "title": "退款政策",
      "content": "用户在购买后 7 天内可以申请无理由退款..."
    }
  ]
}
```

### 5.7 `/knowledge/upload`

用途：上传文件导入知识库。

支持格式：

| 格式 | 说明 |
|------|------|
| `.txt` | 整个文件作为一篇文档 |
| `.md` | 整个文件作为一篇文档 |
| `.json` | JSON 数组，格式为 `[{ "title": "...", "content": "..." }]` |

示例：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

### 5.8 `/knowledge/stats`

用途：查看知识库片段数量。

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/knowledge/stats
```

### 5.9 `/monitor`

用途：查看 Agent 和工具在线指标。

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/monitor
```

返回内容包括：

| 字段 | 说明 |
|------|------|
| `agent_stats` | Agent 调用次数、成功率、延迟、routing_score |
| `tool_stats` | 工具调用次数、成功率、延迟、熔断状态 |
| `active_alerts` | 最近告警 |
| `suggestions` | 优化建议 |

### 5.10 `/eval/run`

用途：运行内置评测，或运行受登记金标数据集驱动的回归评测。

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/eval/run
```

运行 Billing MVP 金标集：

```bash
curl -X POST http://localhost:8000/eval/run \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dataset":"billing"}'
```

运行完整多 Agent 金标集的 dev 或 holdout 切分：

```bash
curl -X POST http://localhost:8000/eval/run \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dataset":"multi_agent","split":"dev"}'
```

普通评测总是写入 `data/eval/reports/`。只有在完整 holdout 通过最终门槛后，才可追加 `"promote_baseline": true` 更新正式基线。

Billing 数据文件位于 `data/golden/`。它们均为待人工审核的合成数据；外部语料的许可和准入状态见 `data/sources/registry.yaml`。

返回内容包括：

| 字段 | 说明 |
|------|------|
| `pass_rate` | 评测通过率 |
| `total` | 评测项总数 |
| `passed` | 通过项数量 |
| `avg_scores` | 平均评分 |
| `regressions` | 回归检测结果 |
| `recommendations` | 优化建议 |
| `failure_groups` | 按意图、目标 Agent、来源、合成状态聚合的失败样本 |
| `results` | 每条评测结果 |

## 6. 使用项目

### 6.1 主对话接口

请求：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我的订单什么时候到？",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

响应示例：

```json
{
  "conv_id": "session_001",
  "response": "请提供订单号，我可以帮您查询订单状态和物流进度。",
  "intent": "query",
  "agent_type": "general",
  "escalated": false,
  "latency_ms": 1234.5
}
```

字段说明：

| 字段 | 含义 |
|------|------|
| `message` | 用户输入 |
| `user_id` | 用户唯一标识，用于隔离记忆和用户画像 |
| `conv_id` | 会话 ID，相同 `conv_id` 表示同一轮多轮对话 |
| `intent` | 识别出的意图 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级/转人工 |
| `latency_ms` | 端到端延迟 |

### 6.2 多轮对话

多轮对话只需要保持同一个 `user_id` 和 `conv_id`。

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "订单号是 A123456",
    "user_id": "user_001",
    "conv_id": "session_001"
  }'
```

系统会从 Redis 读取当前会话最近消息，并从 ChromaDB 读取相关历史和用户画像，拼成上下文传给 Agent。

### 6.3 技术问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "应用登录一直报 401 错误",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

预期会路由到 `technical` Agent。

### 6.4 账单问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "为什么这个月重复扣款了？我要退款",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

预期会路由到 `billing` Agent。

### 6.5 复合问题示例

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "登录报错 401，而且这个月还重复扣款了",
    "user_id": "user_mix",
    "conv_id": "mix_001"
  }'
```

这类问题会触发受控 Supervisor：先生成两个带领域边界的子任务，再按依赖关系
并行或串行执行。技术 Agent 不处理账单结论，账单 Agent 不替代技术诊断；最终
由 `ResponseSynthesizer` 去重并检查明显的数字或允许条件冲突。Planner、调度器
或合成器失败时会保留已有结果并通过 `degradations` 暴露降级状态。

例如“先查询订单状态，然后根据查询结果判断退款资格”会生成依赖：

```text
general_1（查询订单状态）
    ↓
billing_1（根据订单结果判断退款资格）
```

Supervisor 默认最多创建3个子任务，每个子任务独立超时。可配置：

```env
SUPERVISOR_MAX_SUBTASKS=3
SUPERVISOR_SUBTASK_TIMEOUT_SECONDS=30
```

离线评测 Supervisor 控制面：

```bash
make supervisor-eval
```

该评测使用20条内部确定性样本和金标意图，衡量复合检测、Agent 分配、依赖、
升级与控制面延迟；它不调用真实 Agent LLM，因此不会把控制面通过率冒充端到端
回答成功率，也不会生成线上 `synthesis_fallback_rate`。

## 7. 知识库使用

ResolveFlow 的知识库由 `mcp/knowledge_base.py` 管理，底层使用 ChromaDB collection：

```text
knowledge_base_cosine_v1
```

该版本化 Collection 显式使用 cosine 距离。启动时若发现旧的 `knowledge_base`
Collection 且新 Collection 为空，系统会保留旧数据并重新生成向量迁入新
Collection；不会尝试修改 ChromaDB 中不可变的 `hnsw:space`。

首次启动时，如果知识库为空，会自动导入默认客服文档，包括退款政策、订单查询、账户安全、技术故障排查、会员积分、配送说明。

### 7.1 查看知识库统计

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/knowledge/stats
```

响应示例：

```json
{
  "total_chunks": 18
}
```

### 7.2 批量导入文档

```bash
curl -X POST http://localhost:8000/knowledge/add \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "退换货政策",
        "content": "用户在购买后 7 天内可以申请无理由退货，审核通过后 5-7 个工作日退款。"
      },
      {
        "title": "会员权益",
        "content": "金卡会员享受 9 折优惠，生日当月可获得双倍积分。"
      }
    ]
  }'
```

系统会把长文档切成 500 字左右的片段，并写入 ChromaDB。

### 7.3 上传文件导入知识库

上传 Markdown：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@data/demo_docs/troubleshooting.md"
```

上传 JSON：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

JSON 格式必须是数组：

```json
[
  {
    "title": "文档标题",
    "content": "文档内容"
  }
]
```

### 7.4 检索知识库

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/search?query=退款需要多久到账&top_k=3"
```

响应示例：

```json
{
  "query": "退款需要多久到账",
  "results": [
    {
      "title": "退款政策",
      "content": "审核通过后，款项将在 5-7 个工作日内退回原支付账户。",
      "chunk_id": "demo-refund-timeline::chunk::0",
      "document_id": "demo-refund-timeline",
      "score": 0.1459,
      "rrf_score": 0.1459,
      "vector_rank": 2,
      "lexical_rank": 1,
      "vector_score": 0.71,
      "lexical_score": 18.42,
      "retrieval_routes": ["vector", "lexical"],
      "rerank_score": 0.93,
      "rerank_model": "qwen3-rerank",
      "degradations": [],
      "chunk": 0
    }
  ],
  "reranked": true,
  "degradations": []
}
```

`/search` 使用的是完整检索优化链路：

```text
原始查询
  -> LLM 查询改写成多个角度
  -> 每个子查询并行执行 ChromaDB 向量召回和全量 BM25 召回
  -> 按稳定 chunk_id 合并，以 RRF 融合两路排名
  -> 多查询结果按 chunk_id 去重
  -> 对有限候选执行 qwen3-rerank 专用模型精排（失败时保留 RRF 顺序）
  -> 返回 Top-K
```

中文查询使用经内部评测校准的词法路线权重，因为当前
`all-MiniLM-L6-v2` 对中文排序弱于中英混合 BM25；英文查询默认使用等权
RRF，精确错误码、订单号和型号会提高词法路线权重。权重只作用于排名融合，
不会把尺度不同的 BM25 原始分数和向量分数直接相加。

`qwen3-rerank` 需要百炼业务空间地址。可设置 `DASHSCOPE_WORKSPACE_ID`
（默认地域 `cn-beijing`），或直接设置完整的 `QWEN_RERANK_BASE_URL`。API Key
依次读取 `QWEN_RERANK_API_KEY`、`DASHSCOPE_API_KEY`，最后可复用当前 Qwen
聊天模型使用的 `ANTHROPIC_API_KEY`。重排请求超时、鉴权失败或响应非法时，
`degradations` 会包含 `qwen3_rerank_unavailable`，结果保持原 RRF 顺序。

降级路径为：向量不可用时保留 BM25，BM25 不可用时保留向量，Query Rewrite
失败时使用原始查询，LLM Rerank 失败时使用 RRF 顺序。`/search` 的
`degradations` 字段和每个结果中的同名字段可用于排障。

## 8. ChromaDB 在项目中的用途

ResolveFlow 使用三个活跃 ChromaDB collection；升级后还可能保留一个旧知识库作为迁移来源：

| Collection | 模块 | 作用 |
|------------|------|------|
| `knowledge_base_cosine_v1` | `mcp/knowledge_base.py` | 当前 RAG cosine 向量索引；BM25 镜像在进程内增量维护 |
| `knowledge_base` | `mcp/knowledge_base.py` | 只读迁移来源，存在时保留以便恢复 |
| `episodic` | `memory/conversation_memory.py` | 压缩后的历史对话摘要 |
| `user_profile` | `memory/conversation_memory.py` | 用户画像，包含偏好和关键实体 |

数据写入时机：

| 数据 | 写入时机 |
|------|----------|
| `knowledge_base_cosine_v1` | 启动迁移旧数据/导入默认文档，或调用 `/knowledge/add`、`/knowledge/upload` |
| `episodic` | 当前会话工作记忆超过阈值后自动压缩并写入 |
| `user_profile` | 每次 `/chat` 回复后异步提炼并更新 |

## 9. 在 Docker 中查看 ChromaDB 内容

Compose 中 ChromaDB 容器名是：

```text
resolveflow-chromadb
```

宿主机访问端口是：

```text
http://localhost:8001
```

容器内部端口是：

```text
http://localhost:8000
```

### 9.1 查看 ChromaDB 是否存活

宿主机执行：

```bash
curl http://localhost:8001/api/v1/heartbeat
```

容器内执行：

```bash
docker exec -it resolveflow-chromadb curl http://localhost:8000/api/v1/heartbeat
```

### 9.2 查看所有 collection

```bash
curl http://localhost:8001/api/v1/collections
```

如果 ChromaDB 版本返回 tenant/database 相关错误，可以使用 Python 客户端查看，见下一节。

### 9.3 用 Python 客户端查看 collections

进入应用容器：

```bash
docker exec -it resolveflow-app bash
```

在容器里执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
print("heartbeat:", client.heartbeat())

collections = client.list_collections()
print("collections:")
for c in collections:
    print("-", c.name, "count=", c.count())
PY
```

预期可以看到：

```text
collections:
- knowledge_base_cosine_v1 count= ...
- episodic count= ...
- user_profile count= ...
```

### 9.4 查看 `knowledge_base_cosine_v1` 文档内容

```bash
docker exec -it resolveflow-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base_cosine_v1")

data = col.get(limit=10, include=["documents", "metadatas"])
for i, doc_id in enumerate(data["ids"]):
    print("=" * 80)
    print("id:", doc_id)
    print("metadata:", data["metadatas"][i])
    print("document:", data["documents"][i][:500])
PY
```

### 9.5 查询 `knowledge_base_cosine_v1`

```bash
docker exec -it resolveflow-app bash
```

执行：

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("knowledge_base_cosine_v1")

result = col.query(
    query_texts=["退款多久到账"],
    n_results=3,
    include=["documents", "metadatas", "distances"],
)

for doc, meta, dist in zip(
    result["documents"][0],
    result["metadatas"][0],
    result["distances"][0],
):
    print("=" * 80)
    print("title:", meta.get("title"))
    print("distance:", dist)
    print("content:", doc[:300])
PY
```

### 9.6 查看用户画像 `user_profile`

先多调用几次 `/chat`，让系统异步生成用户画像：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "我经常咨询会员积分和退款问题，回答请简洁一点", "user_id": "profile_user", "conv_id": "profile_session"}'
```

等待几秒后查看：

```bash
docker exec -it resolveflow-app bash
```

```bash
python - <<'PY'
import json
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("user_profile")

data = col.get(
    where={"user_id": "profile_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print(json.dumps(json.loads(doc), ensure_ascii=False, indent=2))
PY
```

### 9.7 查看情景记忆 `episodic`

情景记忆只有在当前会话消息数量达到压缩阈值后才会写入。默认阈值在 `MemoryManager.COMPRESS_AT` 中，目前是 15 条消息。

可以连续发送多条消息触发压缩：

```bash
for i in $(seq 1 16); do
  curl -s -X POST http://localhost:8000/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"这是第 $i 条测试消息，我想咨询退款和订单问题\", \"user_id\": \"episodic_user\", \"conv_id\": \"episodic_session\"}" > /dev/null
done
```

查看情景记忆：

```bash
docker exec -it resolveflow-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": "episodic_user"},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 9.8 查看 ChromaDB 持久化文件

ChromaDB 的持久化卷在 Compose 中定义为：

```yaml
volumes:
  chromadb-data:
```

查看 Docker volume：

```bash
docker volume ls | grep chromadb
docker volume inspect resolveflow_chromadb-data
```

查看容器内数据目录：

```bash
docker exec -it resolveflow-chromadb sh
ls -lah /chroma/chroma
find /chroma/chroma -maxdepth 2 -type f | head
```

注意：不建议直接修改这些底层文件。查看和管理数据应优先使用 ChromaDB API 或 Python 客户端。

### 9.9 清空 ChromaDB 数据

谨慎操作。停止服务并删除 volume：

```bash
docker compose down
docker volume rm resolveflow_chromadb-data
docker compose up -d --build
```

如果只想删除某个 collection，可以用 Python 客户端：

```bash
docker exec -it resolveflow-app bash
```

```bash
python - <<'PY'
import chromadb

client = chromadb.HttpClient(host="chromadb", port=8000)
client.delete_collection("knowledge_base_cosine_v1")
print("deleted knowledge_base_cosine_v1")
PY
```

删除后重启应用，`KnowledgeBase` 会在 collection 为空时重新导入默认文档。

## 10. Redis 工作记忆查看

Redis 容器名：

```text
resolveflow-redis
```

进入 Redis：

```bash
docker exec -it resolveflow-redis redis-cli -a resolveflow123
```

查看 key：

```redis
KEYS *
```

工作记忆 key 格式：

```text
wm:{user_id}:{conv_id}
```

会话摘要 key 格式：

```text
summary:{user_id}:{conv_id}
```

查看某个会话最近消息：

```redis
LRANGE wm:user_001:session_001 0 -1
```

查看 TTL：

```redis
TTL wm:user_001:session_001
```

默认 TTL 是 24 小时。

## 11. 查看工作记忆压缩内容

工作记忆压缩发生在 `memory/conversation_memory.py` 中。默认配置：

```text
WORKING_MAX = 20
COMPRESS_AT = 15
```

当同一个 `user_id + conv_id` 的工作记忆达到 15 条消息时，系统会：

```text
旧消息 -> LLM 摘要 -> Redis summary
旧消息摘要 -> ChromaDB episodic
最近 5 条消息 -> 继续保留在 Redis wm 列表
```

日志示例：

```text
工作记忆压缩完成: cli_user/5a076f2b-b607-4339-9e9f-f0399862d366，摘要 19 字
```

其中：

```text
user_id = cli_user
conv_id = 5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.1 查看 Redis 中的会话摘要

进入 Redis：

```bash
docker exec -it resolveflow-redis redis-cli -a resolveflow123
```

查询摘要：

```redis
GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

一条命令快速查看：

```bash
docker exec -it resolveflow-redis redis-cli -a resolveflow123 \
  GET summary:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366
```

### 11.2 查看压缩后仍保留的最近 5 条工作记忆

进入 Redis 后执行：

```redis
LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

一条命令快速查看：

```bash
docker exec -it resolveflow-redis redis-cli -a resolveflow123 \
  LRANGE wm:cli_user:5a076f2b-b607-4339-9e9f-f0399862d366 0 -1
```

说明：

- Redis 使用 `LPUSH` 写入，最新消息在列表前面。
- 代码读取时会 `reversed(raws)` 还原时间顺序。
- 压缩后 Redis 工作记忆列表只保留最近 5 条；更早的内容会以摘要形式进入 Redis summary 和 ChromaDB `episodic`。

### 11.3 查看 ChromaDB 中的情景记忆摘要

如果是全栈部署，应用容器名通常是：

```text
resolveflow-app
```

进入应用容器：

```bash
docker exec -it resolveflow-app bash
```

如果你是用 `docker run --rm` 跑 CLI，容器名可能是随机的。先查看：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Networks}}\t{{.Status}}'
```

进入对应容器：

```bash
docker exec -it <容器名> bash
```

执行 Python 脚本查询 `episodic`：

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"
conv_id = "5a076f2b-b607-4339-9e9f-f0399862d366"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    meta = data["metadatas"][i]
    if meta.get("conv_id") == conv_id:
        print("=" * 80)
        print("metadata:", meta)
        print("summary:", doc)
        print("full_text_preview:", meta.get("full_text"))
PY
```

字段说明：

| 字段 | 含义 |
|------|------|
| `documents[i]` | LLM 生成的历史对话摘要 |
| `metadata.user_id` | 用户 ID |
| `metadata.conv_id` | 会话 ID |
| `metadata.ts` | 写入时间 |
| `metadata.full_text` | 被压缩的原始旧消息前 500 字预览 |

### 11.4 如果只想看某个用户的所有情景记忆

```bash
docker exec -it resolveflow-app bash
```

```bash
python - <<'PY'
import chromadb

user_id = "cli_user"

client = chromadb.HttpClient(host="chromadb", port=8000)
col = client.get_collection("episodic")

data = col.get(
    where={"user_id": user_id},
    include=["documents", "metadatas"],
)

for i, doc in enumerate(data["documents"]):
    print("=" * 80)
    print("metadata:", data["metadatas"][i])
    print("summary:", doc)
PY
```

### 11.5 Redis summary 和 ChromaDB episodic 的区别

| 位置 | 保存内容 | 用途 |
|------|----------|------|
| Redis `summary:{user_id}:{conv_id}` | 当前会话压缩摘要 | 下一次同会话请求直接拼入 prompt |
| ChromaDB `episodic` | 压缩摘要 + metadata | 跨会话按语义检索相关历史 |
| Redis `wm:{user_id}:{conv_id}` | 最近 5 条消息 | 保持当前对话连贯性 |

## 12. Monitor 在线监控

查看监控摘要：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/monitor
```

响应包含：

```json
{
  "agent_stats": {
    "general_0": {
      "total": 10,
      "success_rate": 1.0,
      "avg_ms": 1200.3,
      "monitor_penalty": 0.0,
      "routing_score": 0.836
    }
  },
  "tool_stats": {
    "knowledge_search": {
      "total": 5,
      "success_rate": 1.0,
      "avg_latency_ms": 80.2,
      "consecutive_fails": 0,
      "circuit_state": "closed"
    }
  },
  "active_alerts": [],
  "suggestions": []
}
```

指标含义：

| 指标 | 含义 |
|------|------|
| `total` | 调用次数 |
| `success_rate` | 成功率 |
| `avg_ms` / `avg_latency_ms` | 平均延迟 |
| `routing_score` | Agent 路由评分 |
| `monitor_penalty` | Monitor 根据在线表现写回的降权系数 |
| `consecutive_fails` | 工具连续失败次数 |
| `circuit_state` | 工具熔断器状态，可能是 `closed`、`open`、`half_open` |

Prometheus 页面：

```text
http://localhost:9090
```

## 13. 运行端到端评测

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/eval/run
```

也可以运行受数据治理约束的 Billing 金标集：

```bash
curl -X POST http://localhost:8000/eval/run \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dataset":"billing"}'
```

评测内容：

1. 意图识别准确率和 Macro-F1
2. 调用 Orchestrator 生成真实回复
3. LLM-as-Judge 从相关性、准确性、完整性、有用性打分
4. 与上一次评测结果做回归检测
5. 生成优化建议
6. 对金标数据额外检查 Agent 路由、人工升级、知识使用、禁用表述和多轮记忆一致性

响应示例：

```json
{
  "pass_rate": 0.83,
  "total": 5,
  "passed": 4,
  "avg_scores": {
    "intent_accuracy": 0.875,
    "relevance": 0.88,
    "accuracy": 0.82,
    "completeness": 0.79,
    "helpfulness": 0.85
  },
  "regressions": [],
  "recommendations": [
    "意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据"
  ],
  "results": []
}
```

## 14. 停止、重启和清理

停止服务：

```bash
docker compose stop
```

重启服务：

```bash
docker compose restart resolveflow
```

停止并删除容器，但保留数据卷：

```bash
docker compose down
```

停止并删除容器和数据卷：

```bash
docker compose down -v
```

重新构建并启动：

```bash
docker compose up -d --build
```

## 15. 常见问题

### 15.1 `/health` 返回 503

查看应用日志：

```bash
docker compose logs -f resolveflow
```

重点检查：

- `.env` 是否配置 `ANTHROPIC_API_KEY`
- Redis 是否健康
- ChromaDB 是否健康
- 应用容器是否正在反复重启

### 15.2 ChromaDB 连接失败

查看 ChromaDB 状态：

```bash
docker compose ps chromadb
docker compose logs -f chromadb
curl http://localhost:8001/api/v1/heartbeat
```

应用容器内测试：

```bash
docker exec -it resolveflow-app bash
python - <<'PY'
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
print(client.heartbeat())
PY
```

### 15.3 Redis 认证失败

确认 `.env` 和 `docker-compose.yml` 中使用的密码一致。默认密码是：

```text
resolveflow123
```

测试连接：

```bash
docker exec -it resolveflow-redis redis-cli -a resolveflow123 ping
```

### 15.4 `/search` 没有结果

先确认知识库中有数据：

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/knowledge/stats
```

如果是 0，可以重新导入演示文档：

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@data/demo_docs/sample_knowledge.json"
```

再测试：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/search?query=API如何接入&top_k=3"
```

### 15.5 用户画像查不到

用户画像是异步更新的，并且依赖 LLM 调用成功。排查步骤：

1. 先调用 `/chat`，使用固定 `user_id`
2. 等待几秒
3. 查看 `docker compose logs -f resolveflow` 是否出现 `用户画像已更新`
4. 使用第 8.6 节的 Python 脚本查询 `user_profile`

### 15.6 情景记忆查不到

情景记忆不是每次对话都写入。只有当前会话消息数达到压缩阈值后才写入。默认阈值：

```text
MemoryManager.COMPRESS_AT = 15
```

连续发 16 条以上消息后再查看 `episodic`。

## 16. 推荐验证流程

完整验证可以按这个顺序执行：

```bash
# 1. 启动
docker compose up -d --build

# 2. 健康检查
curl http://localhost:8000/health

# 3. 主对话
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，我想了解退款政策", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. 知识库统计
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/knowledge/stats

# 5. 导入演示知识库
curl -X POST http://localhost:8000/knowledge/upload \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "file=@data/demo_docs/sample_knowledge.json"

# 6. 检索
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/search?query=ResolveFlow如何接入API&top_k=3"

# 7. 监控
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/monitor

# 8. Skills
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/skills

# 9. 评测
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/eval/run
```

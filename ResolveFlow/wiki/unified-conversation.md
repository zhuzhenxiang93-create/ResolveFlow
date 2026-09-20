# 统一会话与执行链路

## 当前入口

`make agent-chat` 默认启用统一会话。可以连续输入问题，无需在咨询后使用 `/new`。
`/session` 查看会话 ID，重启后用 `/conversation ID` 恢复。
`/confirm ID` 才是具体操作确认；普通的“好的”不会代替确认。审批仍由独立入口处理。

HTTP 使用 `POST /agent/conversation`，或 `POST /chat`——`/chat` 现在只有这一条逻辑，不再有
`mode` 字段，旧的 mode=chat（直接调用编排器、绕过身份校验）和 mode=action（直接调用
`execute_action`）分支已下线，见下方“入口收敛”。两者都要求 `role=user` 的 JWT（见
[Agent 执行指南](agent-execution.md) 的 `/agent/auth/token`），不会信任请求中的 user_id——
身份取自 JWT 的 subject。响应返回 conversation_id（/chat 为 conv_id），下一轮需带回该 ID。
现在支持多个不同 subject 的用户各自隔离的会话，但仍是单机本地签发的 JWT，不是生产多租户登录方案。

## 执行流程

1. 服务端确认身份，检查会话归属并取得会话执行锁。
2. 读取近期对话、相关历史和用户画像。
3. 原 IntentRecognizer 输出意图提示；GoalInterpreter 结合当前消息和上下文理解业务目标。
4. 咨询从订阅知识库检索并返回带来源的答案；办理进入原 ActionRuntime。
5. 运行时继续执行权限检查、工具调用、用户确认、独立审批和业务状态核验。
6. 对话和执行结果写入共享记忆。任务、确认、审批、取消、修订和恢复接口同步会话进度。

历史和画像只能用于理解，不能成为身份、业务证据或授权。
待确认期间可以咨询政策；咨询不会改变原任务或批准操作。
模型识别超时返回可重试错误，原任务保持不变，不暗中改用固定业务流程。
结束后可以在同一会话开始下一项任务，不继承旧操作授权。

## 模块与后端

- `agents/conversation_service.py`：统一会话入口、上下文拼接和咨询/执行分流。
- `core/intent_recognizer.py`：复用原识别器，可注入共享模型客户端。
- `agents/goal_interpreter.py`：结构化业务目标；当前轮咨询与旧任务目标分离。
- `agents/action_runtime.py`：接收已解析目标，避免同一轮重复目标理解。
- `memory/local_conversation_memory.py`：CLI 默认 SQLite 记忆，最近 12 条消息、同用户其他会话最近 100 条候选的关键词召回；画像仅记录明确的回复风格偏好。
- 完整 API 启动后注入原 MemoryManager、AgentOrchestrator 和 ToolManager。CLI 不要求 Redis、Chroma 或下载向量模型。
- 本地知识检索复用 KnowledgeBase/BM25，仅使用内部数字订阅资料；完整 API 使用主干 ToolManager 的查询改写+并行召回+RRF+Qwen3 重排链路，并通过 `allowed_document_ids` 在检索阶段（ChromaDB `where` 子句 + 词法候选过滤）而非事后过滤把范围锁定在订阅域文档，不会把主干其他业务文档（如物流、通用退款政策）混入订阅答案；请求异常标记 `full_rag_unavailable` 并回退本地 BM25，请求成功但范围内确无命中标记 `full_rag_no_scoped_hit`，二者含义不同、不合并成一个标记。
- Qwen 配置下不代表三路意图通道全部启用；以实际 source_scores 为准。

### 入口收敛（2026-09-12）

`mode=chat`/`mode=action` 兼容入口已下线。此前三条并行入口——旧编排器直接问答、旧
`execute_action` 直接办理、新的统一会话——现在收敛成一条：`/chat` 只做统一会话分流，
`AgentOrchestrator.run()`/`execute_action()` 这两个底层方法本身没有删除（`ConversationService`
和 `api/action_routes.py` 的 `/agent/tasks*` 直接任务操作接口仍在用），只是不再有绕开身份校验、
绕开 `ConversationService` 的 HTTP 快捷路径。旧客户端如果仍在请求体里发 `"mode": "auto"`
这类字段不会报错（未知字段被静默忽略），但会得到统一入口的行为，不再有三条路径的行为差异。

同一轮鉴权扩展也把 `/search`、`/knowledge/*`、`/skills`、`/monitor`、`/eval/run` 从完全开放
改成分层要求：只读接口（`/search`、`/knowledge/stats`、`GET /skills`）要求任意已登录角色，
写/运维接口（`/knowledge/add`、`/knowledge/upload`、`/skills/reload`、`/monitor`、`/eval/run`）
要求 `role=admin`。`/health`、`/metrics` 保持开放——前者是 Docker/Compose 健康检查依赖的，
后者是 Prometheus 抓取的标准指标端点，两者都不含业务或用户数据。

混合咨询和办理的政策回答仍由运行时的订阅知识工具处理；完整 API 下已真正接入主干混合检索（范围受限，非事后过滤），CLI/无 Chroma 环境仍是本地 BM25-only 回退，两条路径的检索能力不等价。

## 验证与边界

离线验证：`make test`。使用模拟目标/工具、真实 SQLite，不代表模型准确率。
另有 `scripts/check_unified_live.py --live --max-calls N`，显式发起真实调用，N 为 1 到 12
（默认 12 次可以直接用 `make unified-live-check`）。
运行需要 PYTHONPATH 指向 ResolveFlow、现有 Python 环境及 `.env.agent.local`。
该脚本使用隔离模拟订单，拒绝修改工具，测试止于用户确认前。

2026-09-11 本轮实测：

- 最终 `make test`：151 项测试通过，包含统一会话、跨入口共享状态、超时保留任务、咨询插问与权限隔离；`git diff --check` 通过。
- 初次发起 3 次 Qwen 请求，首轮政策返回，第二轮意图请求超时；旧脚本异常退出没有生成 JSON。该失败不能省略。
- 修复异常报告后使用剩余 9 次请求，四轮跑完。咨询、指代办理、补订单、等待确认、无业务修改均满足检查。
- 等待确认时插问退款政策未正确分流，所以完整验收仍为失败（6 项检查中 5 项通过，不是任务完成率）。
- 原始报告：`data/eval/reports/agent/unified-live-/20260911T114030Z.json`，保留 passed=false。
- 随后补充当前轮目标提示、通用咨询边界规则和离线回归。预算已用完，此项修复尚未做真实模型复验。
- 原始模型将首轮咨询误判为修改，由服务端安全规则校正；不能宣称模型独立理解全对。

尚未完成：Redis/Chroma 完整服务实机验收、真实模型修复后复验、记忆保留期与删除接口、生产身份和权限系统。
画像简化版不是丰富客户画像；脱敏规则不是完整敏感数据检测；任务审计仍需单独的数据治理。
意图识别与目标解析仍是两个模型环节，成本与延迟不能隐藏。
总 Token/成本因旧 create 接口缺少用量而标记 null；native_usage_only 仅是部分调用用量。
业务系统仍是模拟系统，真实 Qwen 调用不等于真实扣款或退款。

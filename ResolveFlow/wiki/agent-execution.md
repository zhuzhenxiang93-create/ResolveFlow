# ResolveFlow：受控业务执行 Agent

2026-09-11 更新：action 已新增 LLM 目标理解与独立用户确认，详见
[目标理解与确认](goal-understanding-confirmation.md)。下文相应流程以本次更新为准。

## 定位与边界

面向订阅制数字服务的客服执行原型，研究如何在业务权限内推进任务，而不是仅生成答案。
所有账单、权益和退款均在 SQLite 模拟，无真实支付调用。RAG 仍属于原有问答链路，本轮不扩展检索算法。
这不是 Fin/Zendesk 的替代品，也没有验证商业竞争优势。

| 请求 | 必要信息 | 权限与动作 | 完成条件 | 暂停或失败 |
| --- | --- | --- | --- | --- |
| 政策咨询 | 问题 | 静态模拟政策，不访问订单 | 说明权限规则，不宣称办理成功 | 复杂政策仍需原问答入口 |
| 查询订阅/账单/服务 | 本人订单号 | 仅查询 | 获取对应业务证据；完成查询不等于消除异常 | 无订单则澄清，无权限不泄露订单 |
| 权益修复 | 本人订单号、明确修复请求 | 先查订阅和服务，再同步 | 重新读库：权益匹配套餐且服务健康 | 服务故障转人工，不盲目同步 |
| 重复扣款退款 | 本人订单号、明确退款请求 | 查询后提交一笔重复扣款审批 | 批准后重新读库：净扣款为一笔 | 拒绝、过期、业务变化均不执行 |
| 复合异常 | 同上，分别授权 | 技术与账单分工、依赖核验 | 每个请求目标均完成 | 保留部分完成和未解决项 |

启用模型时初始语义由 GoalInterpreter 解析；离线模式使用标记的保守规则。模型解析不授予权限，实际修改都需用户确认；目标变化必须显式 revise。
模拟账单用扣款/退款笔数表示，没有金额、币种、税费、部分退款或时间窗口政策。

## 执行链路与职责

```text
/chat（统一入口）或 /agent/tasks/continue
  -> 服务端身份解析 + conversation/task 关联检查
  -> AgentOrchestrator.execute_action（入口，不再单独规划）
  -> GoalInterpreter：结构化目标理解（无模型时明确标记离线规则）
  -> action_policy：权限范围、类型化计划与独立业务条件
  -> ActionRuntime：持久化 Supervisor 调度，选取 ready 子任务
  -> 领域角色原生工具调用（技术/账单，共用模型但工具范围不同）
  -> Schema/权限/依赖校验 -> SQLite 模拟工具
  -> 证据、调用结果、检查点 -> 刷新依赖/计划 -> 下一轮
  -> 澄清 / 用户确认 / 审批 / 人工接管 / 独立核验完成
```

原 `supervisor.py` 和 `intent_recognizer.py` 仍服务普通知识问答；action 路径不调用旧的回答子任务规划器。
业务执行的权限、状态、重试只在 ActionRuntime，未在 ToolManager 再实现写操作。
`conversation_memory.py` 的用户画像不是业务授权证据；action 使用持久化任务历史，不将敏感订单写进画像。
这统一了所有 **业务执行入口**，并不意味着问答与动作强行合成一个巨型运行时。

## 有边界的自主性

子任务保存 ID、goal、agent、tool、dependencies、required_information、success_condition。
初始 DAG 来自业务契约；模型可调用 `propose_plan` 重排步骤或添加依赖。
验证器拒绝循环、重复/未知工具、漏掉目标、改成功条件、改领域权限和缺少前置依赖。
模型不能创造新业务工具或扩大授权；这叫受约束规划，不是开放式自主规划。
计划变化有版本及简短理由，模型显式重规划最多 4 次，计划历史最多 32 个版本，任务累计最多 16 轮。

每轮模型只见当前领域的 ready 工具，以及原生调用历史、结构化证据和交接结果。
交接包含证据序号、来源时间、已核验事实、已执行动作、失败和未解决项。
下游调度读取服务器证据，不相信上游自由文本；证据 TTL 为 300 秒且必须匹配订单版本。
事实改变会使依赖重新等待，服务故障阻断同步，账单部分仍可按授权继续。
当前不是独立进程组成的 Agent 团队，交接摘要由执行器生成，不声称验证了多 Agent 效果提升。

## 工具协议与错误

OpenAI function calling 和 Anthropic tool_use/tool_result 统一为带 call_id 的调用与结果。
订单参数通过 Pydantic 严格 Schema；额外 owner、tenant、approved 等参数被拒绝。
同轮最多 4 个调用，按顺序执行，每个都有结果；一个失败不丢弃其他调用。
当前未启用只读并行，以保持 SQLite 证据顺序清晰。修改动作必须再次检查依赖及权限。
无效 JSON 记录原始参数；回放使用合法对象，让两家协议能继续接收参数错误。
重复或缺失调用 ID 整轮拒绝，记录 protocol_error，不污染可恢复工具消息。

错误区分模型超时/请求失败、协议错误、参数错误、权限拒绝、业务前置条件、工具失败和不确定执行。
读工具错误后在任务预算内重试，短退避最高 160ms；同步数据库工具没有网络超时控制器。
模型请求超时 20 秒。SDK 自动重试关闭。模型无调用或提前结束不能绕过状态验收。
业务 API 可保留显式标记的 fallback；验证套件关闭 fallback，模型失败不会计作模型完成。
不保存模型自由文本推理；只记录调用参数、业务证据和简短计划理由。

## 状态、审批与恢复

```text
awaiting_clarification -> running -> completed
running -> awaiting_approval -> running/completed
running -> awaiting_confirmation -> running（用户同意）/cancelled（拒绝）
awaiting_approval -> rejected / needs_human（过期、失效）
running -> needs_human（失败、预算耗尽、依赖受阻）
needs_human -> running（独立审核人 release，且预算尚未耗尽）
非完成任务 -> cancelled / superseded（显式取消/修正）
```

任务、模拟订单、工具历史、计划、审批均持久化。BEGIN IMMEDIATE 保护本地读改写；持久化 lease 避免重复推进，revision 阻止过时模型结果落库。
进程失去 lease 后，新执行者在到期后接管。普通重试不会重启已完成、拒绝、取消、人工持有或待审批任务。
自然语言补充只提取唯一 UUID 订单号，再校验归属；不是通用槽位抽取。多个候选不猜测。
新订单/新目标通过 revise 创建新任务、使旧审批失效；取消不回滚已完成业务。
revise 的取消和创建目前为两个事务，中途异常可能只留下已停止旧任务，可重新创建但未实现原子迁移。

审批绑定具体任务中的审批 ID、订单、动作、笔数、计划版本、订单版本和 900 秒有效期。
绑定摘要仅用于一致性检查，不代替认证签名。审批人由服务端独立凭据决定，不能由模型或用户自批。
批准前重查业务前置条件；重复提交审批不重复退款。失败/超时先读取业务状态核对，不盲目重试写操作。
模拟写入与任务检查点在同一数据库事务，幂等还依赖终态、版本和审批锁；动作中的 key 是追踪标识。
**这不提供跨远程系统 exactly-once**：生产接入还需要业务 API 幂等键、持久化操作账本、状态查询及对账/补偿。
旧 schema 任务拒绝继续，不能复用旧审批；未提供无损旧任务迁移。

## 一键验证

仓库根目录和 ResolveFlow 目录均可运行：

```sh
make test
make agent-demo
make agent-eval
make agent-eval-live
```

报告在 `data/eval/reports/agent/`：独立 JSON run_id 文件、latest_offline.json/md、latest_live.json/md。
不会覆盖原 RAG 指标。每条用例保留工具证据和最终模拟状态，报告记录源码内容哈希及 dirty commit。
12 个自编中文模拟场景是 **开发可见回归集**，不是独立冻结的 holdout，不是官方基准分数。
正常场景评测不注入工具错误，错误修正率标 null；故障注入单测单独验证运行时契约。

真实模型模式只在显式 --live 且配置完整时访问 API：

```sh
export LLM_PROVIDER=openai  # 或 anthropic
export LLM_MODEL='<实际模型名称>'
export LLM_API_KEY='<通过本机环境安全配置>'
export AGENT_EVAL_MAX_CALLS=32
export AGENT_EVAL_MAX_USD='<愿意承担的本次估算预算>'
export AGENT_EVAL_INPUT_USD_PER_M='<服务商实际输入单价>'
export AGENT_EVAL_OUTPUT_USD_PER_M='<服务商实际输出单价>'
make agent-eval-live
```

逐请求预留保守估算费用，失败保留预留额；超限不发新请求。它不是服务商计费硬上限。
缺少配置时只运行离线，live_unavailable 写明原因；Tokens/成本不可得为 null，不伪装成 0。
实际发出的请求与预算拦截前的模型尝试次数分开；estimated_cost_usd 不是账单金额。

## API

独立演示服务：从 ResolveFlow 目录运行 `../.venv/bin/python -m uvicorn api.action_demo:app --port 8010`。
配置 `AGENT_JWT_SECRET`（签名密钥）和 `AGENT_ADMIN_SECRET`（签发令牌用的引导密钥）；AGENT_STATE_PATH 可指定 SQLite 文件。
AGENT_USE_LLM=1 开启 API 模型路径；模型配置同上，但 API 仅有每任务轮数限制，不继承评测费用上限。

身份不再是硬编码的单一 demo 用户/审核人字符串，而是本地签发的 JWT，携带真实 subject 和三选一角色
（`user`/`reviewer`/`admin`），见 core/auth.py。仍是单机共享密钥方案，没有外部 IdP、没有吊销列表。
`admin` 角色现在不只签发 token：`api/main.py` 的知识库写入（`/knowledge/add`、`/knowledge/upload`）、
`/skills/reload`、`/monitor`、`/eval/run` 都要求 `role=admin`，见 [统一会话](unified-conversation.md)
的“入口收敛”一节。

获取一个测试用 token（本地开发用 `make mint-token SUBJECT=alice ROLE=user`，直接用配置的
`AGENT_JWT_SECRET` 本地签发，不发 HTTP 请求，见 scripts/mint_agent_token.py）：

```bash
curl -X POST http://127.0.0.1:8010/agent/auth/token \
  -H "X-Admin-Secret: $AGENT_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"subject":"alice","role":"user","ttl_seconds":3600}'
```

| 路由 | 请求/用途 | 身份 |
| --- | --- | --- |
| POST /agent/auth/token | subject、role、ttl_seconds | X-Admin-Secret（非 JWT 的根信任点） |
| POST /agent/demo/orders | 创建模拟订单 | user |
| POST /agent/tasks | message，可选 conversation_id | user |
| POST /agent/tasks/{id}/continue | order_id 或 message，可选 conversation_id | user |
| GET /agent/tasks/{id} | 持久化状态 | user（仅任务所有者） |
| POST /agent/tasks/{id}/approval | approval_id、approved | reviewer（subject 不能等于任务所有者） |
| POST /agent/tasks/{id}/confirmation | confirmation_id、accepted | user |
| POST /agent/tasks/{id}/cancel | 停止，不回滚 | user |
| POST /agent/tasks/{id}/revise | 新 message，返回新任务 | user |
| POST /agent/tasks/{id}/release | 显式释放人工持有任务 | reviewer |

`/chat` 是唯一对话入口，不再有 `mode` 字段：`{"message":"请修复权益并申请退款"}`，命中业务目标
时返回 action_task，命中知识问答时不返回。继续传 task_id 和 order_id/message；conv_id 必须匹配
任务保存的 conversation_id。
用户传入 user_id 不覆盖服务器身份（身份来自 JWT 的 subject）。现在支持真实的多个不同用户和多个不同审核人
（reviewer 与被审核任务的 owner 必须是不同 subject，否则服务端拒绝），但仍是单机部署，没有真实多租户 IAM、
没有租户隔离、没有令牌吊销。

## 未完成边界

没有真实模型成功率、远程支付对账、操作队列、多租户 RBAC、线上负载和数据保留/脱敏制度。
同步 SQLite 调用的慢阻塞不能用 asyncio 超时强制中止；当前测试用注入异常覆盖超时语义。
已验证实例重建/过期 lease 恢复，但没有实际 SIGKILL、磁盘损坏、跨机器故障演练。
RAG 未接入 action 工具池；政策仅为模拟契约，复杂知识仍走普通 chat。
外部数据未导入，见 external_research.md 和 adoption_decisions.md。

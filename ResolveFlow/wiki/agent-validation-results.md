# 优势融合验收记录

执行日期：2026-09-09。模式：本地 SQLite 模拟 + 确定性场景 + Mock 原生协议测试。
未配置真实模型凭据和费用预算，没有发生真实 LLM 验证或支付操作。

## 实际命令

| 命令 | 结果 |
| --- | --- |
| 仓库根目录 make test | 最终 89 tests，全部通过，4.568 秒 |
| ResolveFlow 目录 make test | 路径入口验证通过；当时 87 tests，之后新增的测试已从根目录同一目标验证 |
| make agent-demo | 三阶段演示成功：澄清 -> 待审批 -> 重建 runtime 后批准并核验完成 |
| make agent-eval | 12 个内部场景，全部符合预期 |
| make agent-eval-live | 命令执行成功，但配置缺失，仅离线验证；报告 requested_mode 与实际 mode 分列 |
| git diff --check | 通过 |

测试中出现现有 Chroma telemetry 参数警告、LibreSSL/urllib3 警告及故障注入日志，不影响测试退出码。
未为本轮 Agent 改造修改无关 RAG 依赖。

## 实测指标

持久报告：`data/eval/reports/agent/latest_offline.json` 与同名 Markdown；
每次运行另有 UUID JSON，保存完整 task/evidence/plan/final_state 和源码内容哈希。

| 指标 | 本次结果 | 正确解释 |
| --- | --- | --- |
| 预期行为通过 | 12/12 | 内部开发可见回归，不是泛化正确率 |
| 业务目标完成 | 7/12，58.33% | 其余为预期澄清/拒绝/人工处理，不是全部失败 |
| 无审核人完成 | 5/12，41.67% | 确定性执行，不是 AI 独立解决率 |
| 应暂停审批 | 5/5 | 暂停前检查未发生退款 |
| 正确人工处理 | 3/3 | 不计入完成 |
| 正确缺参暂停 | 1/1 | 不计入完成 |
| 实例重建状态恢复 | 2/2 | 非真实 SIGKILL 演练 |
| 未授权修改/重复操作 | 0 / 0 | 仅该 12 场景的模拟权益/退款变化和动作标识范围 |
| fallback | 0 | 正常场景套件关闭模型失败回退 |
| 平均/P95 | 7.05ms / 18.61ms | 本机确定性路径，包含 harness 即时审批，不含真实人工/LLM 等待 |
| 模型调用 | 0 | 未配置真实模型 |
| Token/实际费用 | null | 未获得，不能当作零成本模型表现 |
| 工具错误修正率 | null | 正常场景未注入工具错误；相关能力由故障单测验证 |

## 来源到验收

| 参考来源 | 本地连接 | 实际通过的验收 | 当前限制 |
| --- | --- | --- | --- |
| Agents SDK / Fin HITL | runtime approval / API reviewer | 拒绝、过期、绑定变更、伪造审批、自批拒绝、重复批准 | 单 demo 身份，不是企业 IAM |
| LangGraph checkpoint | SQLite lease + revision + tool_messages | 活跃执行并发互斥、过期 lease 恢复、工具消息持久化 | 没有多机器或真实进程 kill |
| AutoGen state contracts | 角色限定工具、结构化 handoffs | 多步证据进入下一轮；依赖失败阻断同步；复合部分完成 | 共用模型，非自治独立团队 |
| Zendesk procedure | typed plan / propose_plan / clarify | 模型重排改变执行顺序、循环依赖拒绝、旧审批失效、自然语言补订单 | 初始目标规则，非开放式业务拆解 |
| τ 独立评分 | 状态 verifier + 独立 evaluator | 工具伪成功不完成；评测器能发现 runtime 虚假完成；写成功响应丢失只核对不重写 | 笔数级模拟，不是支付一致性认证 |
| BFCL / ToolBench 问题分类 | 原生 tool/results + Pydantic | 非法 JSON、额外身份参数、未知工具、重复 ID、同轮部分失败、错误后修正 | Mock 数据，不是官方数据集结果 |
| Langfuse observation 字段 | attempts/report/mode/usage | 指标必要字段、null 语义、费用预留失败保留、预算阻止额外请求 | 未接 tracing 平台，费用只是估算 |

## 完整任务轨迹

本次 agent-demo 的请求是“请修复升级后的权益，并申请重复扣款退款”。

1. 创建任务，识别两个明确授权目标，缺订单号进入 awaiting_clarification。
2. 补充属于当前用户的订单，技术角色查询套餐与权益、服务状态。
3. 在服务健康且权限允许时同步权益，重新读库确认从 basic 变为 pro。
4. 账单角色查询到 charges=2、refunds=0，提交单笔退款审批；此时并未退款。
5. 重建 ActionRuntime，恢复原 task/plan/evidence/approval。
6. 独立审核人批准，重查计划与订单版本，模拟 refunds 变为 1。
7. 最终按两个业务目标分别核验，verification 均为 true，unresolved 为空。

## 文件与职责

- agents/action_policy.py：语义范围、五类工具契约、DAG 校验、目标条件。
- agents/action_runtime.py：唯一业务状态机、角色调度、原生调用循环、证据、lease、审批和状态恢复。
- core/llm_client.py：两家原生工具协议转换、usage、关闭 SDK 隐式重试。
- agents/agent_orchestrator.py：执行入口传递消息、订单及会话，不另做动作规划。
- api/main.py、api/action_routes.py、api/action_demo.py：统一 action 入口、身份、继续/审批/取消/修正/人工释放。
- evaluation/agent_evaluator.py、scripts/run_agent_eval.py：独立状态断言、离线/live 分离、预算、来源和报告。
- data/eval/agent_scenarios.json：12 条内部合成回归场景与 provenance。
- tests/test_action_runtime.py、test_action_safety.py、test_native_tools.py、test_agent_evaluator.py：动作与安全、协议及评测回归。
- scripts/run_agent_demo.py：明确授权的全链路演示。
- data/sources/agent_research_snapshot.json：一次性只读源码/版本核查（scripts/research_agent_sources.py，已清理）留下的固定源码版本和哈希快照，作为调研证据保留。
- 根/backend Makefile、README、wiki 文档：运行入口、业务边界、外部调研和采用理由。

保留进入任务前已有改动，未提交、推送或部署。未新增框架依赖。

## 尚未完成与面试表述

六阶段是模拟范围的实现，不是全部生产要求已满足。细分状态见 adoption_decisions.md。
尚缺真实模型验证、独立 holdout、外部基准适配、通用槽位抽取、远程工具超时/对账、多租户权限、原子 revise 和真实崩溃恢复演练。
外部数据未适配的理由：尚无经过校验的本地政策/工具/状态映射，不将移植后的任务误报为官方得分。

推荐表述：

> 围绕订阅客服的权益与账单异常，设计并实现受控 Agent 执行原型：模型在授权工具与任务依赖内选择动作，高风险退款绑定人工审批，结果通过业务状态独立核验。建立 12 个内部模拟回归场景，全部符合预期，并通过全仓 89 项测试；真实模型效果另行验证。

产品价值是“把可以回答、可以查询、可以执行、必须审批和必须停下分清楚”，
不是“用 Multi-Agent 自动退款”。现阶段不写生产解决率、降本收益、用户量或线上性能优势。

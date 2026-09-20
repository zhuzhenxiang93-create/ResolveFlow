# 优势融合决策

核查日期：2026-09-09。来源、版本和边界见 external_research.md 及源码快照清单。
所有采用项为独立实现的模式借鉴，没有新增 Agent 框架依赖。

| 参考对象 | 原有具体缺口 | 实际吸收设计与模块 | 维护成本 | 验收测试 | 决策 |
| --- | --- | --- | --- | --- | --- |
| Fin / Agents SDK HITL | 暂停/审批与可恢复任务关系不够明确 | action_runtime：审批绑定动作、订单/计划版本、TTL；独立审核身份 | 本地状态机与规则维护；不引入 SDK | expired_and_changed_business_approval、correction_invalidates_old_approval | 采用；事务内复核比只暂停工具更适合模拟退款 |
| LangGraph interrupt | 恢复重跑可能重复推进 | runtime：持久化 lease、revision fencing、终态不可普通重试、审批幂等 | 单 SQLite，非分布式队列 | active_lease_prevents_concurrent_model_execution、restart_expired_lease_and_transcript | 采用模式，不迁移框架 |
| AutoGen save/load | 交接状态缺少稳定契约 | runtime：统一持久化工具历史、证据序号和角色交接 | 同模型不同角色，共用状态；非独立 Actor | argument_correction_and_partial_batch_results、failed_dependency_and_partial_completion | 采用稳定状态契约；维护模式不适合作新增依赖 |
| Zendesk Procedures | 固定关键词流程难以解释缺参与依赖 | action_policy + runtime：类型化 DAG、受验证重排、唯一 UUID 澄清、版本轨迹 | 仅五个业务工具及一个计划工具 | native_replanning_affects_execution_order、plan_change_invalidates_approval_and_cyclic_plan_rejected | 有限采用；未实现开放式计划生成 |
| τ evaluator | 工具成功文案可能冒充业务完成 | action_policy.verify 读真实模拟状态；agent_evaluator 另外断言最终字段/读取证据 | 手写小领域 oracle，扩域需加契约 | false_write_success_not_business_completion、write_response_loss_reconciled_without_retry | 采用；不使用 τ 官方任务或分数 |
| Langfuse observations | 错误、模式、调用量与延迟混在一起 | runtime attempts + agent_evaluator/report：分错误类型、fallback、请求/用量、来源版本 | 本地 JSON，不引入外部数据传输 | report_counts_scopes_and_unavailable_values、failed_requests_retain_reservation | 采用字段模式；平台接入延后 |
| ToolBench / BFCL | 函数 Schema 和多调用错误需要专门验证 | llm_client + runtime：原生两家协议、参数错误回馈、保留每个调用结果 | Pydantic 已有依赖 | openai_and_anthropic_roundtrip、extra_identity_and_duplicate_ids | 借鉴问题划分；数据和外部 API 不引入 |

测试函数完整名称通常以 `test_` 开头，位于 tests/test_action_safety.py、test_native_tools.py、test_agent_evaluator.py。
这些测试验证本地契约，并不证明上游框架同样实现了本项目所有保护措施。

## 不采用和推迟

- 不引入 LangGraph/AutoGen/Agents SDK 运行依赖：当前仅五个模拟工具，已有 SQLite 和原生客户端；迁移会增加回归面。生产多工作节点、长任务或复杂图再评估。
- 不引入外部 Agent 数据：未建立与本地业务状态/政策一致的映射，单纯翻译会制造虚假 benchmark 结果。本轮无外部数据适配脚本；原因不是许可证一概不可用，而是适用性尚未通过。
- 不引入 Langfuse 服务：先保证追踪字段和失败归因可用，未来经脱敏和数据去向评审后接平台。
- 不把自主性扩到任意工具、审批自批、真实退款。服务端业务策略是权限边界，不是模型 Prompt 的建议。
- 不改 RAG 算法、不做前端、不做 SFT/DPO。本轮 action 尚未把知识库暴露为工具，避免夸称完整知识业务融合。

## 六阶段完成程度

1. 工具协议：完成模拟范围的 Schema、身份约束、两家协议适配、多调用结果、错误回馈；未实现通用异步远程工具超时/重试层。
2. 统一执行：完成 action 所有入口共用业务 runtime；有版本化契约计划和模型受限重排。旧知识问答链路保留；不是开放式自动拆解任意业务。
3. 跨轮：完成订单澄清、唯一 UUID 自然语言提取、任务/会话绑定、显式修正和实例恢复；无通用槽位抽取，revise 尚非原子两任务迁移。
4. 可靠执行：完成模拟数据库事务、lease、批准前重查、TTL/版本失效、结果丢失核对和显式人工 release；没有远程 exactly-once、真实进程 kill 或多机器验证。
5. 协作：完成有角色工具范围的调用、服务器结构化证据传递和依赖阻断；共享一个模型/上下文，不是独立自治 Agent 团队，没有验证协作收益。
6. 验收：完成目标级数据库条件、部分完成保留、模型 finish 拒绝绕过；没有生产效果和满意度验收。

因此交付可称“受控 Agent 业务执行原型增强”，不能称“生产级自主客服平台已完成”。

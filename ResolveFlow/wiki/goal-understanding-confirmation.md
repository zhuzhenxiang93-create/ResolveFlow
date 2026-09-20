# 目标理解与用户确认

更新：2026-09-11。本轮不迁移 PostgreSQL，不修改普通 chat 的 19 类意图识别，也不自动切换 chat/action。

## 执行流程

```text
新任务或新的跨轮输入
  -> GoalInterpreter（启用模型时使用原生 interpret_goal 调用）
  -> Pydantic 校验目标、对象、缺失信息
  -> 身份/订单归属校验、建立业务契约计划
  -> 只读工具取得证据
  -> 修改操作就绪：awaiting_confirmation
  -> 用户提交绑定具体操作的确认
  -> 重查版本/权限/业务前置条件
  -> 权益同步，或退款申请 awaiting_approval
  -> 独立审核人批准退款
  -> 数据库状态核验
```

模型理解输入包括当前消息、最近六条任务用户消息、现有目标、订单和未解决项及最近系统提示。
目标输出只允许 entitlement read/repair、billing read/refund、service read、policy read。
症状、政策咨询和操作请求在 Prompt 中区分，否定范围按目标处理；这不是已经实测的语言理解准确率。
模型输出不能包含 authorized、owner、approved 等权限字段；额外字段、未知目标、非法 JSON 都被拒绝。
每次解析最多两次请求，每个任务最多八次解析记录；失败停止自动执行，绝不回退到可写规则流程。
目标解析和执行循环共用原生工具客户端，因此 live 评测的请求/费用预算同时覆盖两者。
无模型时明确记录 interpretations.mode=offline_rules；单测用 Mock 验证语义接口，不冒充真实模型。

## 用户确认不是审批

这一版所有实际修改都要求显式确认，即使原句已经说“请修复”。查询无需确认。
权益已正确时无需重复同步，也就无需请求修改确认。
每次确认只绑定一个操作，不把同意修复权益视为同意申请退款。

confirmation 包括 task_id、owner、order_id、tool、parameters、plan_version、order_version、expires_at、binding、status。
有效期 900 秒。同步提案显示目标套餐；退款提案显示申请一笔重复扣款退款，明确还需人工审批。
确认接受时间 accepted_at、失效时间 invalidated_at 及动作中的 confirmation_id/timestamp 提供审计依据。
摘要用于一致性校验，不替代身份认证。身份仍由服务端签发的 JWT（`role=user`）决定，见 [Agent 执行指南](agent-execution.md)。

```http
POST /agent/tasks/{task_id}/confirmation
Authorization: Bearer <role=user 的 JWT>
Content-Type: application/json

{"confirmation_id":"服务端返回的确认ID","accepted":true}
```

只接受严格 boolean 和预定义字段。审核人角色的 token 不能替用户同意（会被拒绝）；用户角色的 token 不能批准退款。
“好的”“同意”“确认”等文本不会改变状态或授予权限，客户端必须调用结构化确认接口。
确认完成后通过同一 AgentOrchestrator/ActionRuntime 继续，不直接绕开执行链路操作数据库。
退款审批绑定用户确认 ID；审批前再次检查该确认仍有效。

## 更正与恢复

- 待确认/审批期间收到新业务描述或不同订单，暂停任务、使旧确认与审批失效，提示 revise。
- 等待期间仅处理明确的结构化确认；普通文本处理刻意保守，暂不支持任意插话后无缝返回原确认。
- 未暂停时新的语义目标若改变，不静默替换，转人工并要求 revise；revise 停止旧任务，创建新任务。
- 自然语言中的多个订单候选不能让模型任选，需结构化 order_id 明确选择。
- 模型凭空产生、未出现在输入且非已确认对象的订单号被阻断；查库仍校验归属。
- 计划/业务版本变化后旧确认失效；过期或拒绝不执行；重复同意不重复操作。
- 已接受后发送相反的确认值不会推进执行，应使用 cancel 停止尚未执行的任务。
- SQLite 保存目标解析、确认和工具消息，重建实例可恢复。schema_version 升为 3，旧任务保持原文件但拒绝继续，需要创建新任务；没有自动复用旧授权。

## 验证与限制

运行 make test、make agent-demo、make agent-eval。demo 中新增两次具体用户确认。
正常场景套件名称变为 resolveflow-agent-internal-regression-v2-confirmation，沿用 12 条内部回归输入。
评测 harness 根据预定义场景意愿接受/拒绝提案，不根据模型输出自动授权。
新增 user_confirmations、unconfirmed_operations，并检查业务动作关联接受时间及有效期。
tests/action_test_support.py 仅为旧执行器单测模拟明确同意，生产代码不引用它；
tests/test_goal_confirmation.py 直接使用生产 runtime，验证没有确认就不修改等反向场景。

限制：仅原生工具协议和 Mock 流程验收，不代表真实模型理解效果；没有新独立 holdout。
症状型只读任务完成查询后暂不主动提出修复建议，用户可显式发起新操作任务。
缺失信息只支持订单、目标、操作，不是通用槽位系统。用户确认没有前端页面，用 API 提交。
PostgreSQL 迁移留作独立可靠性改造，仍需事务边界、表结构、幂等约束和并发测试，不能只替换连接字符串。

## 本轮结果

全仓 105 项测试通过（包含 16 项直接针对生产运行时的目标/确认测试）；完整 demo 通过。
12 个内部离线回归场景全部符合预期，其中 7 次用户操作确认，未确认修改、未授权修改及重复操作均为 0（仅该场景范围）。
实际运行 agent-eval-live 后确认当前缺少模型、API Key 和费用预算配置，仅执行了明确标记的离线验证；尚无真实模型语义准确率。

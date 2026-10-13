# ResolveFlow 多意图识别与精简 Agent 编排（咨询链路）

本版删除了独立 `TaskPlanner` 和 Orchestrator 的第二轮领域关键词扫描。主路径为：

```text
IntentRecognizer（一次识别多个意图和可选的数据依赖）
  → AgentOrchestrator（白名单映射、同领域合并、生成 TaskPlan）
  → DAGScheduler（校验 DAG、调度并行/依赖任务）
  → 领域 Agent（独立 Prompt / Skills / Tool Scope）
  → ResponseSynthesizer（合并成功/失败、冲突与降级）
```

## 1. 数据契约

LLM 一次结构化输出：

```json
{
  "intents": [
    {"intent": "technical_login", "text": "登录一直失败", "confidence": 0.91, "depends_on": []},
    {"intent": "logistics", "text": "查一下快递", "confidence": 0.88, "depends_on": []}
  ],
  "reasoning": "登录故障与物流查询相互独立"
}
```

`intent` 来自 `IntentCategory`，`text` 必须是本轮用户消息中的连续原文片段。
最多接受四个意图，且每条需通过置信分数与证据检查。无效的、缺乏用户原文依据的
候选意图将被丢弃。模型不得把否定请求转为需执行的操作；但该提示限制仍须通过
数据评测，绝不能用作业务操作授权。

`depends_on` 为**依赖先前意图结果**的 0 基下标列表（缺省空列表）。
例如“先查订单，再根据订单结果核验退款资格”，第二个意图依赖第一项：
`depends_on: [0]`；如果只是“先排查登录、然后查快递”，无业务数据依赖，
两项均为 `[]`，允许并发。解析器校验下标、顺序与引用；DAGScheduler 再
校验无未知节点、无重复任务及无环。

旧版单意图 `intent`/`intent_group`/`confidence` 仍用于已有单意图调用方；
只在模型输出旧 JSON 或 LLM 失败时执行旧版单标签加权融合兜底，
避免新的主路径再做一次关键词或向量分类。

## 2. 简化后的职责

1. **IntentRecognizer：** 一次解析用户明确提出的多个独立诉求、片段与结果依赖。
2. **AgentOrchestrator：** 使用静态 `IntentCategory → AgentType` 白名单，
   同领域多个意图合并为一个 Agent 任务；按原文片段生成脱敏的 `TaskPlan`，
   不进行第二次意图识别或关键词拆句。紧急/明确转人工优先路由 Escalation。
3. **DAGScheduler：** 校验 Agent 白名单、任务数、依赖引用、无环和超时；
   同一轮中无依赖且前置条件满足的任务并发运行。若组合后的任务依赖无效，
   Orchestrator 安全降级到仅处理主领域，并返回 `task_plan_unavailable`。
4. **ResponseSynthesizer：** 纯确定性合并、去重、冲突和失败提示，无附加
   LLM 汇总调用。

同一个 Agent 的多个意图当前合并执行，减少重复模型调用。对于交叉领域的
复杂依赖，由显式 `depends_on` 转换为领域 DAG 边；涉及同一领域中多个
相互依赖且又跨其他领域的任务，合并可能形成循环，需作为后续评测与拆分优化点。

`/chat` 在普通咨询路径返回 `recognized_intents`、主辅 Agent、`task_plan`
与 `execution_trace`；`recognized_intents` 仅回显意图标签、分数和依赖索引，
不回显敏感原文片段。

## 3. 业务安全边界

统一 `/chat` 会先由 `CommerceConversation` 区分商品、订阅和业务写操作；
本改造只重构其后的普通咨询 Agent 路径。退款、权益恢复、关闭续费仍在
Commerce/Action Runtime 的确定性操作、用户确认、人工审批、幂等及状态核验
机制内完成。模型的结构化意图及依赖信息只用于理解任务，不能成为写入授权。

## 4. 离线评测与局限

新增 `data/eval/supervisor_multi_intent_cases.jsonl`（20条内部合成、多意图
标签迁移案例，尚未经独立人工审定），用于**给定金标意图**的路由及 DAG
控制面验证；旧版 `supervisor_cases.jsonl` 作为历史基准保留。
新的 `run_supervisor_eval.py` 读入多意图案例，不依赖 TaskPlanner。

从 `ResolveFlow/` 目录（安装依赖后）运行：

```bash
python -m unittest discover -s tests -p 'test_multi_intent_routing.py' -v
python -m unittest discover -s tests -p 'test_supervisor.py' -v
python scripts/run_supervisor_eval.py
```

这些测试不能证明在线模型能准确识别多个意图。正式评估前需要额外人工标注
多意图数据，测 Intent Set Match、每意图 Precision/Recall、否定指令误触发、
依赖判断正确率、最终任务完成率、P95 延迟和调用成本，且需单独回归资金操作安全。

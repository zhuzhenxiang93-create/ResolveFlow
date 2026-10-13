# ResolveFlow 多意图识别与 Agent 编排（咨询链路）

本设计将 **跨领域复合咨询** 的意图发现提前到 IntentRecognizer。输入一次用户消息，直接获得多个经过字段校验的意图。原有的 CommerceConversation / Action Runtime 仍然负责商品、订阅、退款等需要对象消歧、用户确认和独立审批的业务流程。

## 1. 数据契约

模型返回：

```json
{
  "intents": [
    {"intent": "technical_login", "text": "登录一直失败", "confidence": 0.91},
    {"intent": "logistics", "text": "查一下快递", "confidence": 0.88}
  ],
  "reasoning": "包含登录故障和物流查询两个诉求"
}
```

- `intent` 必须来自已定义的 IntentCategory 枚举。
- `text` 必须是**当前用户消息中的连续原文片段**，用于任务范围和证据约束。
- 只接纳当前阈值以上、0–1 范围内的分数；最多四个意图。
- LLM Prompt 要求排除被否定的操作以及历史消息中的已结束请求。**此约束还依赖模型正确理解否定语义**，不能把它当作真实写操作的授权依据。
- 旧版 `IntentResult.intent`、`intent_group` 和 `confidence` 保留兼容，表示第一个有效意图；新增 `IntentResult.intents` 表示完整意图列表。
- 旧模型若只返回单一 `intent`，原有 LLM / 本地 n-gram / Pattern 加权融合仍可作为单意图兼容路径；不能由该路径推断未识别的其他诉求。

## 2. 编排

1. IntentRecognizer 一次识别多个意图。
2. Orchestrator 根据静态的 `IntentCategory -> AgentType` 映射选择 Agent。相同 Agent 的多条意图合并为一个领域请求。明确转人工或 CRITICAL 紧急度会覆盖主 Agent 选择。
3. TaskPlanner 以意图对应的原文片段生成各领域子任务，限定任务范围；普通任务依赖仍由当前规则解析。
4. DAGScheduler 验证 DAG 并调度依赖已满足的任务，同一批 Ready 任务可并发执行。
5. ResponseSynthesizer 用确定性规则整理已返回的结果，保留超时和失败信息。

`/chat` 在普通咨询路径中返回 `recognized_intents`、`primary_agent`、`supporting_agents` 和任务 Trace 元信息，便于验证和调试。

## 3. 安全与局限

- 意图识别**只是语义解析，不授予 API 写权限**。写操作继续由 CommerceConversation / Action Runtime 校验授权对象、金额、有效证据、确认、审批和幂等。
- 当前普通咨询编排沿用确定性的 DAG 依赖推断，主要依赖 “先…然后/再” 等标记；下一步可考虑显式依赖和经校验的模型提案。
- 当前模型提供的置信度没有做概率校准；需要用真实多意图测试集评估 Intent Precision/Recall、Exact Set Match、否定操作误触发率、路由正确率、任务完成率及延迟/成本。
- 统一对话入口会先调用商务场景解释器；**本次改造只替换其后的通用多 Agent 咨询链路**，不改变订单与订阅业务的受控执行路径。
- 为兼容直接调用 Orchestrator 的旧测试代码，未提供 `intents` 的老接口仍保留历史路由启发式逻辑；提供 `intents` 的新路径不再用第二次关键词扫描补造意图。

## 4. 运行测试

从 `ResolveFlow/` 目录（安装项目依赖后）：

```bash
python -m unittest discover -s tests -p 'test_multi_intent_routing.py' -v
python -m unittest discover -s tests -p 'test_supervisor.py' -v
```

后续建议对 `/chat` 的普通咨询路径进行多意图端到端测试，并单独回归商品、订阅、退款等业务操作场景。

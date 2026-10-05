# ResolveFlow · 产品案例与简历描述

## 要解决的问题

客服对话可以生成建议，但涉及退款、续费和权益时，仅有一段“处理成功”的回复不够。
用户需要知道操作对象、金额、影响和真实状态；系统需要阻止越权、误选对象和重复执行。
ResolveFlow 将售后交互拆为 Answer、Query、Action，并让可核查业务状态成为结果依据。

这是基于模拟购买记录的项目设计与验收，不包含真实客户访谈、线上运营数据或转化率结论。

## 用户旅程与产品取舍

| 环节 | 设计 | 原因与代价 |
|---|---|---|
| 初次体验 | 自动签发隔离的 Demo 身份，提供五个场景入口 | 减少 JWT 配置门槛；匿名签发仅存在于独立 Demo 服务 |
| 请求理解 | 模型提取意图，模糊退款展示候选 | 多一次选择换取对象确定性 |
| 决策 | 先展示报价、影响及时间线，再请求确认 | 增加显式步骤，让高影响操作可理解 |
| 授权 | 独立 reviewer 审核退款 | 展示职责分离；角色切换只是一种演示方式 |
| 执行 | 后端校验状态，支付/履约明确标记 simulated | 验证控制链，不伪装真实支付接入 |
| 复合咨询 | 业务路径处理金额，Technical Agent 独立处理 401 | 避免技术建议与交易权限混在一起 |
| 故障/无模型 | 错误提示、离线规则、可恢复 Case | 降低演示依赖，同时保留能力边界 |

## 技术上如何保证这些承诺

- [CommerceStore](../ResolveFlow/business/commerce.py) 决定资格、整数分金额、对象归属和状态迁移。
- [CommerceConversation](../ResolveFlow/business/conversation.py) 把语义提议转换成对象明确的业务请求；模型输出本身不是授权。
- [ConversationService](../ResolveFlow/agents/conversation_service.py) 保留业务结果，再处理独立技术咨询及工具追踪。
- [Demo host](../ResolveFlow/api/portfolio_demo.py) 使用每会话 SQLite、不同 user/reviewer JWT，不签发 admin。
- [ActionCard](../ResolveFlowFrontend/src/components/ActionCard.vue) 呈现服务端金额、影响、状态及可执行按钮，不在浏览器计算退款资格。

## 可核查的成果

[验收报告](validation.md)记录后端 313 项中 304 通过、9 项可选依赖跳过；Commerce 专项 42/42。
[真实模型证据](live-validation.json)记录 Qwen Plus 三项 smoke checks，包括实际调用
`lookup_error_code` 和 `knowledge_search`。这不是“配置了密钥即算接通”。

[真实截图](screenshots/README.md)展示 ¥259 耳机退款、独立审核、Completed、¥99 Pro 重复扣款和 Technical 回答。
浏览器验收覆盖刷新恢复、身份 A/B 切换、重复点击及四种屏幕宽度。

## Product Evaluation 与 Bad Case 闭环

功能演示只能证明“能跑通”，产品评测要回答的是：用户要求的事情有没有真正办成，以及高风险操作是否始终受控。

- **评测对象**：招聘 Demo 当前主链 `/chat → ConversationService → CommerceConversation → CommerceStore`，经 HTTP 和真实 user/reviewer JWT 回放。旧的 ActionRuntime 评测保留，但不作为产品评测对象。
- **数据集**：80 条 Ground Truth（单任务 12、对象消歧 10、多意图 10、多轮 10、业务规则 8、确认/审核 10、幂等/状态 8、权限安全 7、政策/技术工具 5），另有 16 条 holdout。
- **判定**：最终结果读取每个会话的 SQLite 业务表和审计日志。确认合规由审计事件的先后顺序证明（用户 confirm → 非本人 reviewer approve → 写入）。
- **Bad Case**：每个失败 Case 按有限枚举归类，root cause 只在有证据时给出，否则标为 NEEDS_REVIEW。
- **闭环**：第一次评测 60.0%（48/80）。32 个 Bad Case 中，Wrong Action 14、Task Planning 6、Missing Clarification 5、Response Quality 4。主要根因是离线规则漏抽意图（11 个），其次是写请求被降级成查询（4 个）、多目标漏掉一个（4 个），以及同名订单被自动选中（1 个）。按根因修复后，主集 100%（80/80），修复前写好的 holdout 从 12.5% 提升到 93.8%。全过程中，Confirmation Compliance 都是 100%，Unauthorized Operations 都是 0。

详见 [Product Evaluation 报告](product-evaluation.md)；Demo 页面的 **Evaluation** 标签读取的是同一份报告 JSON。

## 简历描述：AI 产品经理方向

**ResolveFlow｜AI 客服与受控售后 Agent 应用**

项目链接：https://github.com/zhuzhenxiang93-create/ResolveFlow

- 将客服需求拆解为 Answer / Query / Action，设计政策问答、对象澄清、退款与停续费五类演示场景，形成用户确认—独立审核—结果核验的售后闭环。
- 设计退款金额、业务影响、操作时间线与角色切换交互，降低首次体验的身份配置门槛，并明确离线降级和模拟支付边界。
- 建立场景验收与证据记录，完成浏览器退款闭环、刷新恢复、身份隔离及桌面/移动端检查；真实 Qwen Plus 复合请求可同时呈现重复扣款报价与技术排查结果。
- 设计 80 条 Ground Truth 的 Product Evaluation（覆盖对象消歧、多意图、多轮、确认/审核、幂等、越权 9 类场景），以 SQLite 持久化业务状态判定任务成功，定义 Task Success、Object Resolution、Confirmation Compliance 等 8 项指标并搭建 Evaluation Dashboard。
- 基于自动 Bad Case 归因（离线规则模式）将 Task Success Rate 从 60.0% 提升至 100%；修复前预留的 16 条 holdout 从 12.5% 提升至 93.8%；全程 Confirmation Compliance 100%、越权操作 0。

## 简历描述：AI Agent / LLM 应用方向

**ResolveFlow｜Vue 3 + FastAPI 的受控业务 Agent 应用**

- 构建 LLM 意图解析与确定性业务规则分离的应用链路，通过对象归属校验、报价版本绑定、用户确认、独立审核与幂等状态迁移控制模拟退款操作。
- 接入真实 Qwen Plus 和只读错误码/知识检索工具，拆分 Commerce 与 Technical 复合请求，保留工具追踪和诊断；隔离 Demo 使用 BM25 词法检索。
- 使用会话独立 SQLite 与 user/reviewer JWT 实现身份隔离；本地后端回归 313 项中 304 通过、9 项可选依赖跳过，Commerce 专项 42/42 通过。
- 实现基于真实 HTTP 链路回放的产品评测框架（fixture、攻击步骤、审计顺序校验、规则化 Bad Case 分类、只读报告 API），并按根因修复离线意图解析与对象消歧，离线规则模式下主集 Task Success 从 60.0% 提升到 100%、holdout 从 12.5% 提升到 93.8%。

评测数字须同时注明“离线规则模式、模拟业务数据、自建评测集”。不要只写“100%”：主集修复后的结果是在同一数据集上复测的，必须和 holdout 一起写。
以上写法只描述仓库可证明的工作。个人职责、项目周期与团队规模应按实际经历填写；不要添加未经证实的用户量、营收、效率提升比例、线上 SLA 或“完全自主退款”等表述。

## 面试时建议展示的证据

1. “我要退款”只展示候选，不能自动确认。
2. ¥259 由后端报价；user 无法执行 reviewer 的审批接口。
3. 重复点击后只有一笔模拟退款，刷新后 Case 仍在。
4. Pro 重复扣款金额为 ¥99；401 由独立 Technical Agent 处理。
5. 打开 **Evaluation** 标签：先看 Baseline 的 32 个 Bad Case 和根因，再看修复前后对比和 holdout。
6. 明确回答局限：无真实支付接入、无本次完整混合 RAG 验收；产品评测在离线规则模式下运行，模型输出仍有不确定性。

[演示讲稿](demo-script.md) · [本地运行](local-development.md) · [返回首页](../README.md)

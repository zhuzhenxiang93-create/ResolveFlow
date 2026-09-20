# 产品定位：电商平台付费会员体系客服

核查日期：2026-09-12。来源、局限性写法延续 `external_research.md`/`adoption_decisions.md` 的规范——
真实访问的产品页面/研究文章、写清楚"厂商自述"和"独立验证"的区别，不采信未经核实的数字。

## 一句话定位

> ResolveFlow 面向的是"电商平台 + 付费会员体系"（对标京东PLUS、淘宝88VIP、Amazon Prime 这类
> 模式），不是纯数字订阅 App。广泛的电商客服话题（物流、商品退货、发票、账户安全、技术故障）
> 走多 Agent 协同问答；会员体系专属的高价值、高风险操作（会员套餐权益、会员费重复扣款退款、
> 自动续费管理）走真正的、有用户确认+独立人工审批+数据库状态核验的受控执行。

## 核心场景（产品说明的锚点，不是抽象架构描述）

> 小李是"XX商城"的 PLUS 会员，升级会员后发现包邮权益没生效——找客服。系统先理解这是具体账户
> 问题，查出会员套餐=PLUS、已生效权益=普通会员（付费成功但后台同步失败，真实场景里常见的故障
> 类型），同时查服务健康度排除故障导致不敢乱改。系统提出"是否同步权益"，小李确认后才真正同步，
> 同步完重新读库核验包邮权益确实生效了——不是"模型说改完了就算改完"。小李又提到会员费这个月被
> 重复扣款，系统查账单确认后提交退款申请，但不会立刻执行，转给独立审核人批准。同一个对话里，
> 如果小李问"我买的商品什么时候到"，走的是另一条腿——普通物流查询，两边不冲突，因为管的本来
> 就不是一回事。

这个场景本身就是 `wiki/agent-validation-results.md` 里 `agent-demo` 已经跑通的真实流程
（"请修复升级后的权益，并申请重复扣款退款"），不是为了讲故事新编的场景。

## 市场调研：真实产品怎么划这条边界

以下均为 2026-09-12 实际检索到的公开资料，厂商自述能力和独立验证是两回事，不采信具体
ROI/准确率数字作为本项目的结论，只借鉴"边界怎么划"这个设计判断。

| 产品 | 定位 | 目标客户 | 广泛话题 vs 执行能力的边界 |
| --- | --- | --- | --- |
| [Intercom Fin](https://www.intercom.com/help/en/articles/7120684-fin-ai-agent-explained) | 覆盖客户生命周期全流程的 AI 客服/销售 agent，2026年6月被 Salesforce 以约36亿美元收购并入 Agentforce | 中大型企业，8000+客户（含 Anthropic/DoorDash/Mercury） | 广覆盖优先，执行能力随企业自定义程度扩展 |
| [Sierra](https://sacra.com/c/sierra/) | 自称"Agent OS"——明确写"不只是回答FAQ，而是真的能处理退款、改账户、挽留取消订阅" | Fortune 50 中 40%+，2026年5月按 158亿美元估值融资9.5亿美元 | 高度定制化部署（非自助注册，年费200K+美元），执行范围按客户逐个共建 |
| [Decagon](https://decagon.ai/product/overview) | 全渠道会话式AI客服平台，Agent Operating Procedures (AOP) 让非技术团队配置流程 | "快速增长的数字原生公司"（零售/旅游/fintech/edtech） | 自称70-80%自动化率，但"复杂退款计算仍需人工复核"——即使是执行类操作也留了人工兜底 |
| [Zendesk Resolution Platform](https://www.zendesk.com/blog/zendesk-insights/innovation/relate-2025-resolution-platform-ai-agents/) | 明确把结果分三类：Unassisted / Assisted Escalation / Automated Resolution；advisory（知识/建议）和 action（真正执行）是两个不同能力层级 | 全市场，按 resolution 结果计费而非坐席数 | 用 LLM 自证"是否解决"来验证结果——ResolveFlow 现在读真实模拟数据库状态核验，比这个更严格 |
| Ada（据 [Fini Labs 对比文章](https://www.usefini.com/guides/ai-platforms-for-refunds-returns-billing-disputes)转述） | 端到端自动处理账单对话，不转人工 | 高工单量 SaaS | 完全自动化，不强制人工审批——比 ResolveFlow 的设计更激进，本项目刻意不跟随这一点 |
| Fini（YC，据同一篇转述） | 每笔退款对照实时订阅状态、政策版本、客户权益推理，宣称98%准确率零幻觉 | SaaS 账单场景 | 和 ResolveFlow 的"证据必须新鲜（TTL 300秒）+ 订单版本校验"设计思路一致，未采信其准确率数字 |

**行业策略共识**（据多篇2026年客服自动化ROI研究文章转述，非本项目独立验证）：
"最成功的客服自动化项目从一个narrow use case开始……先把资源集中在单一最高ROI流程上，
不要同时铺开三个"；且"订阅账单是SaaS客服里ROI最高的自动化品类"被独立多次提及。这两点
共同支持了 ResolveFlow 现有设计（只有会员体系这一个域有真正执行能力）是一个有意为之、
市场验证过的取舍，而不是能力不足的妥协。

**开源架构参考**：[Rasa](https://rasa.com/) 目前主推的 CALM 引擎明确写"LLM 负责理解，
业务逻辑和动作选择留在代码里，不让 LLM 决定"——这和 ResolveFlow 现有的
`GoalInterpreter`（LLM 理解）→ `agents/action_policy.py`（代码里写死的类型化 DAG 校验，
模型不能创造新工具或扩大授权）架构模式一致，是本领域公认的做法，不是本项目独创。

## 目标客户

对标 Decagon 那一档：**中小型、快速增长、以电商/会员业务为核心的公司**，想优先自动化
"会员费/权益"这类高频高价值工单，但（不同于 Ada 的"完全自动化不转人工"）更看重可审计、
可解释——涉钱的操作必须有独立人工签字，不是把全部信任交给模型。适合对合规、财务对账
敏感的团队，不是追求"零人工介入"指标的团队。

## 能力边界（回答"广泛话题要不要执行能力"）

| 话题类型 | 处理系统 | 能力 | 例子 |
| --- | --- | --- | --- |
| 商品物流/配送 | 老系统（`AgentOrchestrator`）| 纯咨询，不执行 | "我买的商品什么时候到" |
| 商品退货退款 | 老系统 | 纯咨询；`GoalInterpreter` 的 `unsupported_requests` 显式标记为不支持执行，引导走普通订单售后 | "这个商品我要退货" |
| 发票、账户安全、技术故障 | 老系统 | 纯咨询，多 Agent 协同处理复合请求 | "发票抬头怎么改" |
| 会员套餐与权益 | 新系统（`GoalInterpreter`+`ActionRuntime`）| 查询 + 用户确认后同步修复 | "我升级PLUS了权益没生效" |
| 会员费重复扣款 | 新系统 | 查询 + 用户确认 + 独立人工审批 + 核验 | "会员费这个月扣了两次" |
| 自动续费 | 新系统 | 查询 + 用户确认后关闭，保留当前权益 | "帮我关闭自动续费" |

这个边界不是能力缺口，是市场验证过的"先窄后宽"策略的具体体现：广泛话题不需要执行能力，
是因为这类平台本来就该有纯咨询的客服面；会员体系单独做执行能力，是因为这是独立研究
证实的最高ROI自动化品类。

## 两份"退款政策"为什么不冲突

`data/knowledge/subscription_service_v1.json` 的 `subscription-refund-v1`（会员费退款）
和主干知识库的通用退款政策（商品退货退款）不是同一件事的两份不一致说法，而是**两个不同
业务流程各自的政策**——就像京东PLUS会员费退款和普通商品退货走的是完全不同的客服流程和
团队。两份文档需要在文案上互相点明各自的范围（见 Phase B 的具体文案改动），不需要退休
其中一份或强行合并成一个数据库。

## 开源数据集调研（已发现，未导入）

| 数据集 | 相关度 | 许可证 | 状态 |
| --- | --- | --- | --- |
| [Bitext Media LLM Chatbot Training Dataset](https://huggingface.co/datasets/bitext/Bitext-media-llm-chatbot-training-dataset) | SUBSCRIPTION 类 8 意图（cancel/change/renew/subscribe/premium/free_trial/subscription/subscription_prices）与 `action_policy.py` 业务目标高度吻合；其余7类（CONTENT/FUNCTIONING/PROGRAM_SCHEDULE/SETTINGS等）是流媒体播放器专属场景，不适用 | CDLA-Sharing-1.0（share-alike，衍生数据需同许可证发布）| 已发现，未导入。24,627条问答对，8类25意图，纯英文，`response` 字段是 Bitext 自己的话术模板不能直接用，`instruction` 字段的10种语言变体标注（口语/礼貌/否定/错别字等）可用于压力测试问法多样性，但需要先翻译成中文并重新标注成本地 `GoalProposal` schema，不是拿来即用 |
| [Schema-Guided Dialogue (SGD)](https://github.com/google-research-datasets/dstc8-schema-guided-dialogue)，Google Research | 20个服务域的"schema+slot+API调用"标注对话，含 Media 域；架构方法论参考价值大于直接数据复用价值 | CC BY-SA 4.0 | 已发现，未导入。用于对照检验 `GoalInterpreter` 的 schema 设计方法论是否符合业界任务型对话数据集的标注规范，不作为训练/评测数据源 |
| [Strova AI customer support conversations](https://huggingface.co/datasets/strova-ai/customer_support_conversations_dataset) | 号称覆盖 SaaS 垂类多轮合成对话 | 未查到明确许可证 | 已发现，未验证。许可证和数据质量都还没有核实，不能作为结论引用 |
| Bitext 通用客服数据集、BFCL、API-Bank、τ-bench、ToolBench | 见 `wiki/external_research.md`/`adoption_decisions.md` | 各不相同 | 项目之前已评估，结论为"暂不导入"（缺本地业务映射或版本/许可证边界未锁定）；这次定位调整未改变这些结论 |

## 已解决：复合请求（老系统多 Agent 编排 + 新系统 Action 执行的合并）

原先的问题：一句话同时混"广泛话题"和"会员操作"（例如"帮我查物流，另外会员费退了没"）时，
`GoalInterpreter` 只识别得出会员那半句，"物流"那半句会被整体丢弃，用户连"处理不了"的提示
都收不到。已解决：`GoalProposal` 新增 `general_remainder` 字段标出业务目标之外的独立通用
请求；`ConversationService` 在 `action`/`knowledge` 两条路由都会把已算好的业务结果（Action
执行结果或政策 RAG 回答）包装成 `SubTaskResult`，通过 `AgentOrchestrator.run_compound()`
接入老系统本来就有的 `TaskPlanner`/`DAGScheduler`/`ResponseSynthesizer` 复合请求处理机制，
对剩余部分做规则化拆分、并行调度并合成回复——支持任意 N 层嵌套（例如"物流+技术故障+退款"
三件事一句话），因为老系统的子任务拆分/调度本身就不限定子任务数量。`ResponseSynthesizer`
新增 `extra_results` 参数，让预先算好的业务/政策结果参与最终内容合成，但不参与关键词冲突
检测——避免业务结果里的真实数字和无关话题的巧合关键词被误判成"数字/极性冲突"从而触发不
必要的人工升级。单话题消息路径不变，不产生额外模型调用；只有真正的复合消息才会调用
`run_compound()` 多花一次意图识别。

同时解决了一个相关但独立的问题："退款"本身的指代歧义（会员费退款 vs 商品退款）。当消息里
"退款"没有任何一边的限定词时（`agents/subscription_knowledge.py:refund_target_is_ambiguous`），
用真实订阅状态（`ActionRuntime.has_subscription()`）判断而不是让模型猜：没有订阅记录的用户
一定是在问商品退款，直接重定向到老系统的通用处理；有订阅记录则是真歧义，直接反问用户，而
不是赌一个方向。这个判断只在会话没有已建立上下文（没有进行中的任务）时生效，避免覆盖模型
已经用会话上下文正确判断过的结果。

代码位置：`agents/goal_interpreter.py`（`general_remainder` 字段）、`agents/supervisor.py`
（`extra_results` 参数、`_HEADINGS` 新增 `action`/`policy`）、`agents/agent_orchestrator.py`
（`run_compound()`）、`agents/conversation_service.py`（`_maybe_compound()`、退款歧义守卫）、
`agents/action_runtime.py`（`has_subscription()`）。测试：`tests/test_compound_requests.py`、
`tests/test_supervisor.py` 新增用例，`tests/test_subscription_rag_scope.py` 更新为使用带
明确限定词的消息（该测试关注 RAG 范围隔离本身，不是歧义判断）。

**真实模型验收（已完成）**：新增 `data/eval/compound_acceptance_v1.json`（7 个场景：纯业务/
纯政策/纯通用/两种两段复合/三段复合/复合中混 unsupported）+ `scripts/run_compound_acceptance.py`
（复用 `run_p0_acceptance.py` 的预算受限 `BudgetClient` 模式，独立预算账本，不触碰已冻结的
`p0_acceptance_v1.json`）。首轮验收发现一个真实的、与本次改动无关但此前从未被测过的问题：
纯通用消息（"帮我查一下物流"，不含任何订阅内容）会被模型错误地补一个 `entitlement: read` 目标
——因为此前 `GoalInterpreter` 从未被喂过"完全不含订阅内容"的消息（旧数据集里所有场景都或多
或少沾订阅边），这次新增的 `general_remainder` 测试场景是第一次真正测到这个边界。修复方式是
在系统提示里明确"空 goals 是正常且正确的结果，不要为了凑一个分类就默认补 entitlement/billing"，
修复后 7/7 全部通过；同时从已冻结的 `p0_acceptance_v1.json` 用 `--only` 回归了 3 条风险最高的
场景（`out-of-scope`——验证收紧后的 `unsupported_requests` 描述没有把"物流查询"错判成支持范围
之外；`policy-refund`——验证退款政策咨询分类不受影响；`combined-approved`——验证"修复权益并
申请退款"这个复合业务 demo 场景不受退款歧义守卫误伤），3/3 通过。

**人工手动测试中又发现一个真实问题（已修复）**：用户在前端实测"商品退款政策是什么"，本应和
"帮我查一下物流"一样走 `general_remainder`（纯通用问题，无订阅内容），结果被判成
`unsupported_requests`。原因是"退款政策"这几个字太容易让模型联想到 `policy:read`（这个字段
被明确限定为"只认订阅"），一旦发现不是订阅的政策就直接判"我回答不了"。收紧 prompt 后，反复
调用同一句话发现**模型输出本身不稳定**（4 次里 1 次对、3 次错，把 `policy:read` 错误地安到一个
明确带"商品"限定词的问题上）——纯靠 prompt 改不出可靠的稳定性。最终方案是在 `agents/subscription_
knowledge.py` 新增确定性函数 `policy_refund_is_actually_goods()`（和已有的 `refund_target_is_
ambiguous()` 共享同一套"会员侧/商品侧限定词"正则），在 `ConversationService._send()` 里加一层
服务端强制纠正：只要文本里有"商品/订单"等限定词、模型却仍然分到 `policy:read`，就无条件剥离
这个分类、导流回老系统——不依赖模型这次到底猜没猜对。加了确定性单测锁定（`tests/test_compound_
requests.py` 的 `PolicyRefundIsActuallyGoodsTests` + `test_goods_policy_misclassification_is_
corrected_to_chat`），并用真实模型跑了 4 次同一句话确认路由 100% 收敛到 `chat`。全部单测
（230 个）复跑通过，复合请求验收数据集（7/7）和冻结集回归（3/3）重新跑过确认无回归。

**Docker 端到端验证（部分完成，发现一个环境相关问题）**：两段式复合（action+通用、policy+通用）
在真实 Docker 部署下两次验证均干净通过，回复正确分节、无误报冲突。三段式复合（物流+技术故障+
退款）在 Docker 里两次尝试都只出现两个分节（缺物流部分），但直接在宿主机（不经 Docker）用完全
相同的代码/模型跑同一条消息，两次都完整拿到三个分节，且 `execution_trace` 显示两个子任务都是
真实成功——排除了路由/调度/合成逻辑本身的问题。已确认该场景需要 `run_compound()` 内两个真实
模型子任务并发执行，而两段式场景只有一个子任务、不触发并发；这指向 Docker 容器出站网络路径在
并发发起两个真实模型请求时的不稳定性，不是本次改动引入的逻辑缺陷。已尝试把 `DAGScheduler` 的
单任务超时从 30s 提到 60s（该项通过既有的 `SUPERVISOR_SUBTASK_TIMEOUT_SECONDS` 环境变量调整，
未改代码），未能解决，问题不在超时。留待后续：如果需要在 Docker 里稳定复现三段式场景，需要单独
排查该容器的 DNS/出站网络配置。

## 尚未解决、明确不在本轮范围内的问题

- 本文档的市场数据全部来自二手转述文章，不是从各厂商一手财报/产品文档逐条核实的数字，
  不能当作本项目的准确率/ROI 基准，只用于支撑"边界怎么划"这个设计判断。
- 目标客户画像和定价假设均为基于公开资料的合理推测，未经过真实用户访谈验证。

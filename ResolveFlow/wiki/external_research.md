# 外部调研：2026-09-09

本轮实际访问官方产品文档、GitHub API、固定 commit 源码和 HF 原始数据卡。
日期是本次核查日期，不保证页面所有功能在每个套餐/地区均可用。
厂商能力描述、源码存在、实际部署可靠性是三种不同证据。
未登录付费产品实测，未执行外部仓库代码，不采信网页中的执行指令。

## 产品对标

| 产品 | 当前核实的能力与目标客户 | 状态与证据边界 | 对本项目的启示 |
| --- | --- | --- | --- |
| Fin AI Agent / Intercom | 面向客服团队，业务系统连接、带上下文交接；页面描述流程中暂停请同事审批后继续 | 当前在售产品页面；具体能力为厂商描述，未验证套餐资格和取消/退款幂等契约；不采用其解决率作比较 | 审批暂停不等于整段对话永久移交，审批后需要恢复原任务 |
| Zendesk AI agents，generative procedures | 面向已有工单/CRM 流程的服务团队；参数收集、API/CRM 动作、知识步骤和升级；缺参暂停，升级后流程结束 | 官方使用文档已描述；本次页面没有 EAP 标注，但不能推断所有旧账户已开通；未核实通用审批回滚接口 | 区分缺参、动作结果和人工升级，并分别统计 |
| Ada AI Agent | 面向自动化客户服务；Processes/Actions、系统交接和工具级 Coaching | 官方操作文档，非本地实测；本次未找到足以证明通用审批版本绑定/恢复语义的资料 | 限制工具自主执行，追踪工具级失败，而不只看回答文案 |

来源：[Fin 产品与人机协作](https://www.intercom.com/ai-chatbot)、
[Zendesk Procedures](https://support.zendesk.com/hc/en-us/articles/10473649691418-About-generative-procedures-for-AI-agents)、
[Zendesk 升级策略](https://support.zendesk.com/hc/en-us/articles/8357756604186-Configuring-escalation-strategies-and-flows-for-AI-agents)、
[Zendesk 指标说明](https://support.zendesk.com/hc/en-us/articles/10677925692698-Announcing-changes-to-AI-agent-reporting)、
[Ada Process management](https://docs.ada.cx/docs/automation/processes/process-management)、
[Ada Handoff management](https://docs.ada.cx/docs/handoffs/handoff-management)、
[Ada Coaching tools](https://docs.ada.cx/docs/optimization/coaching/coaching-tools)。

完成判定：厂商的 conversation resolution/containment 与本项目数据库目标达成不是同一口径。
这些资料支持“任务执行与受控自主性是合理产品方向”，不证明 ResolveFlow 有竞争壁垒。
本项目更准确的定位是：订阅客服领域、可审计和可复现的受控执行原型。
真实竞争力仍需用户访谈、现有客服流程成本、复杂政策覆盖和真实任务完成表现验证。

## 开源实现核查

完整 commit、源码 URL、源码 SHA256、最近推送、release API 返回值在
`data/sources/agent_research_snapshot.json`。以下不是只读 README 得出的设计建议。

| 项目与固定版本 | 实際阅读的实现/示例 | 核实内容、维护与采用判断 |
| --- | --- | --- |
| LangGraph，0199b519e1f9 | libs/langgraph/langgraph/types.py 的 interrupt | 中断需 checkpointer；恢复会重跑节点，不能把恢复等同 exactly-once。MIT，最近推送 2026-09-08；release API 返回 sdk==0.4.4，不把它称作 Python runtime 最新版。学习 checkpoint/replay，不安装框架 |
| OpenAI Agents SDK，e3a03edf186b | examples/agent_patterns/human_in_the_loop.py | needs_approval、RunState 序列化及批准/拒绝后 Runner 恢复。MIT，v0.22.1，2026-09-08。借鉴审批中断；本地额外绑定业务版本，不复制示例代码 |
| AutoGen，027ecf0a379b | python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_base_group_chat.py 的 save_state/load_state | 稳定参与者名、团队状态保存；源码警告运行中快照可能不一致，load 时拒绝运行中状态。最新 API release python-v0.7.5（2025-09-30）；当前 README 明确 maintenance mode，不作新依赖推荐。代码 MIT、文档 CC-BY-4.0，GitHub 根 license 字段不等于代码许可证 |
| Langfuse Python SDK，d839e642534d | langfuse/_client/client.py 的 start_as_current_observation | trace、observation、version、usage/cost/status 参数。MIT，v4.15.1（2026-08-28）。借鉴结构化追踪字段，暂不接外部平台，避免多一个部署和隐私边界 |
| tau2-bench 仓库，672227c6b667 | src/tau2/evaluator/evaluator.py | 按环境状态、动作、沟通、自然语言等独立评估，再合成奖励。MIT，release v1.0.1（2026-07-22）。注意当前主 README 已介绍 τ³-bench，不能只凭仓库名称它为“最新 τ²” |
| ToolBench，d56fdd89faf8 | toolbench/inference/Downstream_tasks/rapidapi.py | API Schema 转换、Finish、give_up_and_restart、调用约束；其工具环境与本项目审批账本不同。Apache-2.0，最近推送 2025-05-21，无 latest release 返回。借鉴失败分类，不连接 RapidAPI |

官方入口：[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、
[Agents SDK 审批](https://openai.github.io/openai-agents-python/human_in_the_loop/)、
[AutoGen 维护与许可证](https://github.com/microsoft/autogen)、
[Langfuse SDK](https://github.com/langfuse/langfuse-python)、
[τ 系列当前仓库](https://github.com/sierra-research/tau2-bench)、
[ToolBench](https://github.com/OpenBMB/ToolBench)。

已核实的兼容/评测风险：LangGraph 恢复重跑节点；AutoGen 活跃运行的状态一致性及维护模式；
τ v1.0.1 的 release notes 记录评分修正和 fixture 答案泄漏清理，因此仅标数据集名不够，必须固定 revision。
[τ release notes](https://github.com/sierra-research/tau2-bench/blob/main/RELEASE_NOTES.md)。
本次 GitHub LangGraph issue 搜索页、Langfuse tracing 文档页访问失败，没有声称完成所有未解决 issue 审计；SDK/项目未来升级仍需兼容测试。

## 数据集与基准适配判断

| 来源 | 已核实适用范围 | 数据/版本与限制 | 本轮决定 |
| --- | --- | --- | --- |
| τ 系列，以上 pinned commit | 任务、政策、工具环境及状态验收，最接近多步服务业务 | 不是生产真实中文客服日志；当前仓库覆盖范围超过早期 τ²，版本更新影响评分 | 吸收独立状态判定，未映射电信/零售工具到本地笔数账单，因此不导入任务、不报官方分数 |
| BFCL HF 数据卡与作者榜单 | 函数选择/参数、多轮缺参，多种用例来源 | HF 卡标 en、Apache-2.0，README latest 日期仍为 2024-09-22，而作者当前榜单为 V4，二者不可混同。数据卡提示不能直接用 load_dataset | 暂缓导入；先解决本地原生协议和权限契约，未来选固定 revision 的多轮子集 |
| API-Bank，作者官方目录 | 工具增强对话及 API 调用评测 | 本次未逐一核实原始任务文件和子数据许可证，不能将仓库整体许可直接用于任务再分发 | 不导入，不宣称已接通评测 |
| ToolBench，以上 pinned commit | 广泛 API 调用和轨迹，不是订阅客服审批标准 | 工具接口及外部 API 可变；代码 Apache-2.0 不自动授权所有第三方 API 数据 | 不引入外部调用或训练数据 |
| BANKING77 / Bitext | 意图分类/客服文本 | 本地来源注册已记录 taxonomy 参考，未见需要重复导入的理由；不具备本次必需的审批状态 oracle | 保持原用途，不算 Agent 执行评测 |

原始来源：[BFCL 数据卡](https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard/raw/main/README.md)、
[BFCL 当前作者榜单](https://gorilla.cs.berkeley.edu/leaderboard.html)、
[API-Bank 作者仓库](https://github.com/AlibabaResearch/DAMO-ConvAI/tree/main/api-bank)。

本轮 **没有外部数据接入**，故没有伪装成数据适配的复制脚本，也没有再分发许可不明文件。
实际接入的是 12 条内部自编中文模拟场景，来源信息在 `data/eval/agent_scenarios.json`。
它们是开发中使用的回归集，不是先冻结后调参的独立测试集，不能声称无测试泄漏的泛化效果。
后续接入外部测试前必须按 original_id/同源家族冻结 split，翻译/改写不能跨 split，保留 source/version/license/language/adaptation_method。
公开 gold 只给评分器，不给 Agent；本地 harness 只把 message 和业务证据送进执行器。

## 证据与署名

本地采用设计思想并独立实现，未拷贝上游源码/任务正文，未增加上游运行依赖。
研究脚本只将源码下载到临时目录供阅读，仓库保存元信息与哈希，不保存上游代码。
如以后直接复用代码，必须随分发保留对应 MIT/Apache 声明及必要 NOTICE；AutoGen 文档与代码分别核对。

# Interactive Agent CLI

数字订阅新增咨询、续费查询和关闭自动续费，见 [售后闭环](subscription-after-sales.md)。
旧模拟订单可能没有续费状态，请使用 /seed 新建，不会将未知字段猜为已开启。

从仓库根目录或 ResolveFlow 目录执行 `make agent-chat`。
自动读取后端 `.env.agent.local`，环境变量优先。默认真实模型，缺少凭据报错，禁止自动 fallback。
每次推进可产生多次模型调用，沿用运行时步骤上限及超时；目前没有 CLI 会话级费用硬上限。

这是可信本地开发入口，复用 AgentOrchestrator.execute_action 和 ActionRuntime，固定 demo-user。
它不需要启动 HTTP 服务，不是生产身份认证边界。业务仍是模拟 SQLite 数据。
默认与本地 API 共用 data/agent/state.sqlite3，可通过 AGENT_STATE_PATH 更换数据库。

首次输入 `/seed` 创建模拟订单，保存显示的订单号。直接输入自己的问题，按追问补充订单号。
修改前显示具体对象和确认 ID，使用 `/confirm 确认ID` 或 `/reject 确认ID`。
普通“好的”不视为确认。退款仍等待独立审批；本 CLI 不提供审批指令。
独立 API 审批完成后 `/continue` 刷新或继续。

`/status` 显示当前任务、证据、已执行动作、验证结果和执行模式。
`/resume 任务ID` 只加载已有任务，不自动执行。`/continue` 才推进任务。
`/revise 完整新目标` 使旧任务失效并建立新任务；`/new 问题` 新建但不取消旧任务。
`/cancel` 取消当前任务，不回滚已经完成的业务动作。`/quit` 或 Ctrl-D 退出并保留任务。
Ctrl-C 中断时应先恢复并检查状态，运行中的租约可能需要等超时后才能继续。

离线测试：在后端目录执行 `PYTHONPATH=. ../.venv/bin/python scripts/agent_cli.py --offline`。
此模式清楚标记离线规则，不代表真实模型效果。CLI 展示的安全摘要来自运行时，不宣称全部文案由 LLM 生成。

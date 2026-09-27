# ResolveFlow

**电商会员售后场景的 LLM Customer Service Agent｜Agent Routing · Controlled Workflow · RAG · Evaluation**

ResolveFlow 面向付费会员售后场景，把“咨询类客服”和“需要改变业务状态的会员操作”放进同一个会话入口。用户可以询问政策、订单、物流、发票和技术问题，也可以发起退款、关闭自动续费等操作；系统通过 Agent 路由处理信息型请求，并通过确认、审核和确定性业务规则约束状态变更。

## Product at a glance

| 维度 | 设计 |
|---|---|
| **User problem** | 售后问题经常同时包含政策咨询、订单信息、技术故障和会员操作，用户需要在一次对话里完成理解、查询和处理。 |
| **Product flow** | 用户请求 → 意图与对象解析 → 咨询类请求分配给领域 Agent / 操作类请求进入受控 Workflow → 用户确认 → 审核 → 执行与状态回显。 |
| **AI role** | LLM 负责理解请求和生成自然语言回复；Technical Agent 可调用只读工具；政策问答通过检索获得上下文。 |
| **Deterministic layer** | 退款金额、资格判断、权限、状态更新和重复提交控制由后端业务逻辑处理。 |
| **Reliability** | 报价绑定订单版本；确认与审核分离；重复提交不会重复退款；购买、物流和支付均为模拟数据。 |

## Recruiter 2-minute tour

1. 按 [运行说明](docs/local-development.md) 启动 Demo，进入用户视角。
2. 输入“把 Pro 会员重复扣的钱退掉，而且登录一直报 401”，观察系统同时生成退款确认卡和 Technical Agent 的排查建议。
3. 完成用户确认后切换审核员视角，查看审核、状态变化和完整业务流程。

[运行说明](docs/local-development.md) · [演示步骤](docs/demo-script.md) · [更多截图](docs/screenshots/README.md)

![ResolveFlow](docs/screenshots/01-home.jpg)

[运行说明](docs/local-development.md) · [演示步骤](docs/demo-script.md) · [更多截图](docs/screenshots/README.md)

## 功能

- 查询商品和订阅政策，展开回答中的来源。
- 查看当前用户的订单、支付、物流、发票和售后记录。
- 申请商品退款、退还重复扣款，或关闭订阅自动续费。
- 在同一次对话中处理售后申请和技术问题。
- 切换用户与审核员视角，查看申请进度和处理结果。

例如，输入“把 Pro 会员重复扣的钱退掉，而且登录一直报 401”，系统会生成一张 ¥99 的退款确认卡，同时由 Technical Agent 查询错误码并给出排查建议。

![重复扣款申请与技术问题处理](docs/screenshots/06-mixed.jpg)

退款需要先确认，再审核；商品退款还需要退货验收。这里的购买记录、物流和到账结果都是模拟数据，没有连接真实支付渠道。

## 快速开始

需要 Python 3.11+、Node.js 20.19+ 或 22.12+。以下命令适用于 macOS / Linux。

```bash
git clone https://github.com/zhuzhenxiang93-create/ResolveFlow.git
cd ResolveFlow

python3.11 -m venv .venv
.venv/bin/pip install -r ResolveFlow/requirements.txt
npm ci --prefix ResolveFlowFrontend
```

在仓库根目录启动后端：

```bash
AGENT_USE_LLM=0 make portfolio-demo PYTHON="$(pwd)/.venv/bin/python"
```

另开一个终端启动前端：

```bash
cd ResolveFlowFrontend
VITE_DEMO_MODE=true npm run dev
```

打开 [localhost:5173](http://localhost:5173)。页面会自动创建用户和审核员身份，并加载八条购买记录。点击 **Reset Demo** 可以开始新的会话。

不配置模型也能体验政策查询和售后操作，此时请求由规则解析，技术问答不可用。要启用 LLM，在后端配置 `AGENT_USE_LLM=1`、`LLM_API_KEY`、`LLM_MODEL`，以及对应的 `LLM_PROVIDER` 和 `LLM_BASE_URL`。项目已用 Qwen Plus 验证，具体启动方式见[运行说明](docs/local-development.md)。

## 实现

前端使用 Vue 3 + Vite，后端使用 FastAPI。Demo 的业务记录和会话记忆保存在 SQLite 中，每个会话使用独立数据库；用户与审核员分别使用不同的 JWT。

一次售后请求经过以下模块：

```text
/chat
  → ConversationService     协调业务请求与其他咨询
  → CommerceConversation    解析意图、确定操作对象
  → CommerceStore           计算报价、校验权限、更新状态
```

如果没有说清要退哪一笔，系统会先列出候选对象。报价绑定订单及其版本，过期或业务状态变化后需要重新确认。确认与审核是两个步骤，重复提交不会生成第二笔退款。

独立的技术咨询通过 `AgentOrchestrator` 分配给 Technical Agent，使用错误码查询等只读工具。Demo 的政策检索使用 BM25；完整后端另有 Redis、ChromaDB 和混合检索配置，见[后端文档](ResolveFlow/README.md)。

<details>
<summary>架构图与目录</summary>

![架构图](docs/architecture.svg)

```text
ResolveFlow/              Python 后端
  api/                    API 与独立 Demo 服务
  agents/                 对话协调、Agent 与工具
  business/               订单、报价和售后规则
  tests/                  后端测试
ResolveFlowFrontend/      Vue 前端
docs/                     运行说明、演示与测试记录
```

当前业务入口是 `api/portfolio_demo.py`，正式服务入口是 `api/main.py`。历史 `ActionRuntime` 保留兼容与只读查询，不再负责业务写入。

</details>


[前端文档](ResolveFlowFrontend/README.md) · [后端文档](ResolveFlow/README.md)

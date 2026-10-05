# ResolveFlow

ResolveFlow 是一个用 Vue 3 和 FastAPI 编写的 AI 客服项目，支持政策问答、订单查询、退款和订阅管理。

项目里实现了一套模拟售后流程。用户可以在聊天中提出退款，查看报价，确认申请，再切换到审核员完成审核。LLM 用于理解请求和回答问题，退款金额、资格判断和状态更新由后端处理。

![ResolveFlow](docs/screenshots/01-home.jpg)

[运行说明](docs/local-development.md) · [演示步骤](docs/demo-script.md) · [Product Evaluation](docs/product-evaluation.md) · [更多截图](docs/screenshots/README.md)

## 功能

- 查询商品和订阅政策，展开回答中的来源。
- 查看当前用户的订单、支付、物流、发票和售后记录。
- 申请商品退款、退还重复扣款，或关闭订阅自动续费。
- 在同一次对话中处理售后申请和技术问题。
- 切换用户与审核员视角，查看申请进度和处理结果。

例如，输入“把 Pro 会员重复扣的钱退掉，而且登录一直报 401”，系统会生成一张 ¥99 的退款确认卡，同时由 Technical Agent 查询错误码并给出排查建议。

![重复扣款申请与技术问题处理](docs/screenshots/06-mixed.jpg)

退款需要先确认，再审核；商品退款还需要退货验收。这里的购买记录、物流和到账结果都是模拟数据，没有连接真实支付渠道。

## Product Evaluation

招聘 Demo 自带一套面向产品结果的评测。80 条脚本化对话会经过真实的 `/chat → ConversationService → CommerceConversation → CommerceStore` 链路，并使用真实的 user/reviewer JWT。是否成功由 SQLite 中持久化的业务状态和审计日志判定，不看回复文本里有没有“处理成功”。

| 指标（offline rules，80 条） | 第一次评测 | 针对 Bad Case 修复后 |
|---|---|---|
| Task Success Rate | 60.0%（48/80） | 100%（80/80） |
| Object Resolution Accuracy | 72.6%（53/73） | 100%（73/73） |
| Multi-intent Completion | 25.0%（3/12） | 100%（12/12） |
| Confirmation Compliance | 100%（19/19 次写入） | 100%（28/28） |
| Unauthorized Operations | 0（11/11 次越权尝试被拦截） | 0（12/12） |

修复是对照这 80 条的失败 Case 做的，所以“修复后”在同一数据集上会高估效果。另有 16 条在修复前就写好、修复过程中没有参考过的 holdout 改写：Task Success 从 12.5%（2/16）提升到 93.8%（15/16），剩下 1 条失败也保留在报告里。

以上结果均为离线规则模式，没有调用模型；实时模型模式可以用 `make product-eval-live` 自行复现。页面右上角的 **Evaluation** 标签（或 `#evaluation`）会读取已生成的报告 JSON，打开页面不会触发评测。完整指标定义和逐条 Bad Case 见 [Product Evaluation 报告](docs/product-evaluation.md)。

```bash
make product-eval PYTHON="$(pwd)/.venv/bin/python"   # 重新生成 80 条主集与 16 条 holdout 报告
```

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

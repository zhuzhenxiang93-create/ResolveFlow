# 部署在线 Demo

部署后，面试官打开一个公开链接，就能和 Demo 对话，走一遍退款 → 确认 → 审核流程，也能查看 Evaluation 页面。
前端和后端打包在同一个容器里，不需要单独配置 CORS 或 API 地址。

```text
浏览器 ──► /                 Vue 构建产物（VITE_DEMO_MODE=true）
        └► /api/python/...   隔离的 Demo API（api.portfolio_demo，经由 api.portfolio_site）
```

## Render 免费方案（推荐）

1. 用 GitHub 账号登录 [render.com](https://render.com)。
2. 依次点 **New → Blueprint**，选择 `zhuzhenxiang93-create/ResolveFlow` 仓库。Render 会读取根目录的 `render.yaml`。
3. 点 **Apply**。首次构建大约需要 5–10 分钟：先安装前端依赖、执行 Vite 构建，再安装 Python 依赖。
4. 构建完成后会得到 `https://resolveflow-demo.onrender.com` 这样的地址。如果服务名被占用，Render 会自动加后缀。
   - 打开 `/` 是客服 Demo。
   - 打开 `/#evaluation` 是评测 Dashboard。
   - 访问 `/api/python/health` 应该返回 `"demo": true, "public": true`。

免费方案的限制：

- 15 分钟没有访问，服务会自动休眠。下一次访问需要等大约 30–60 秒唤醒。发链接给面试官前，自己先打开一次。
- 磁盘是临时的。服务重启后，所有 Demo 会话都会清空，签名密钥也会重新生成。前端检测到旧身份失效时，会自动创建新会话。
- 内存上限 512 MB。实测进程常驻内存约 135 MB。

## 公开模式的保护（`RESOLVEFLOW_PUBLIC=true`）

| 设置 | 默认值 | 作用 |
|---|---|---|
| `RESOLVEFLOW_SESSIONS_PER_HOUR` | 20 | 每个 IP 每小时最多新建多少个 Demo 会话 |
| `RESOLVEFLOW_CHATS_PER_MINUTE` | 30 | 每个 IP 每分钟最多发送多少条消息 |
| `RESOLVEFLOW_SESSION_TTL` | 86400 | 会话的保留秒数，超时后自动清理 |
| `RESOLVEFLOW_MAX_SESSIONS` | 300 | 同时保留的会话文件上限，超出时删除最早的 |

这些限制只在单个进程的内存里计数，作用是防止免费实例被随意刷请求，**不是生产级身份系统**。
公开模式下，旧会话的审计数据只保留到 TTL 到期，这一点和本地 Demo 不同。

## 是否接入真实模型

公开链接默认使用 **离线规则**（`AGENT_USE_LLM=0`），不需要 API Key，也不产生调用费用。
页面横幅会显示“Offline rules demo”。技术类错误码由只读工具查表回答，页面会标注这一点。

如果要在公开链接上接入 Qwen，在 Render 的 Environment 页面设置以下变量：

- `AGENT_USE_LLM=1`
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_PROVIDER=openai`
- `LLM_BASE_URL`

接入前请注意：

- 任何人都可以消耗你的模型额度。建议先在模型平台设置预算上限。
- 可以按需调低 `RESOLVEFLOW_CHATS_PER_MINUTE`。
- 打开公开链接时，模型 Key 只存在于服务端环境变量里，不会下发到浏览器。

## 本地验证同一个镜像

```bash
docker build -f Dockerfile.demo -t resolveflow-demo .
docker run --rm -p 8000:8000 resolveflow-demo
# 打开 http://localhost:8000
```

不用 Docker 的话：

```bash
(cd ResolveFlowFrontend && VITE_DEMO_MODE=true npm run build)
cd ResolveFlow && RESOLVEFLOW_DEMO_MODE=true RESOLVEFLOW_PUBLIC=true AGENT_USE_LLM=0 \
  PYTHONPATH=. python -m uvicorn api.portfolio_site:create_site --factory --port 8000
```

## 发给面试官时建议附上的说明

> 在线 Demo：<链接>（首次打开可能需要约 1 分钟唤醒）
> 业务数据、支付和物流都是模拟的。默认使用离线规则，不调用大模型。
> 右上角 **Evaluation** 可以查看 80 条产品评测、Bad Case 归因和修复前后对比。

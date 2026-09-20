# ResolveFlow Frontend

独立 Vue 前端项目，连接 ResolveFlow Python 后端。

项目目录：

```text
ResolveFlowFrontend/
```

## 功能

- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。
- 支持 Docker + Nginx 部署。
- 接入 Action 执行层，可在页面里演示完整业务闭环——生成模拟订单、发起查询/办理请求、
  查看结构化任务卡（状态、计划步骤、未完成目标）、提交用户确认、以独立审核员身份批准/
  拒绝退款、取消或修订任务。

### 使用 Action 层需要的身份令牌

Action 接口要求本地签发的 JWT（见 ResolveFlow 后端的 [Agent 执行指南](../ResolveFlow/wiki/agent-execution.md)）。
在侧栏「身份令牌」面板分别填入：

- **用户 Token**：`role=user` 的 JWT，用于聊天、查询、确认、取消、修订。
- **审核员 Token**：`role=reviewer` 的 JWT，且 subject 必须与用户不同——服务端会拒绝审核员
  与任务所有者相同的自批请求，这也是页面把两个 Token 拆成两个输入框、而不是共用一个的原因。

本地签发示例（ResolveFlow 目录下）：

```bash
make mint-token SUBJECT=alice ROLE=user
make mint-token SUBJECT=bob ROLE=reviewer
```

## 默认后端地址

默认连接 `http://localhost:8000`。

开发模式下，Vite 会把 `/api/python` 代理到 `http://localhost:8000`。

Docker 模式下，Nginx 会通过 `host.docker.internal` 访问宿主机上的 Python 服务。

## 本地运行

安装依赖：

```bash
npm install
```

启动：

```bash
npm run dev
```

访问：

```text
http://localhost:5173
```

如果后端端口不是默认值，可以启动时覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:8000 npm run dev
```

## Docker 部署

先构建前端静态文件：

```bash
npm run build
```

再构建并启动容器：

```bash
docker compose up -d --build
```

访问：

```text
http://localhost:5174
```

停止：

```bash
docker compose down
```

# SQLite → PostgreSQL：Action 层迁移方案与 POC

## 范围声明

这是技术方案 + 一个独立 POC，**不是生产切换**。`agents/action_runtime.py`（694 行，事务、lease、
审批、确认、恢复全部生产逻辑所在）没有改动，继续跑 SQLite。POC 是新增的隔离文件
`agents/action_runtime_pg_poc.py`，只实现三件事的最小闭环——建任务、拿锁（lease 等价物）、
带幂等键写入——用来在真实 Postgres 上证明"事务边界"和"幂等约束"这两个具体属性能落地，
不是把 694 行代码原样搬过去。

## 现状问题

`ActionRuntime` 现在的并发/幂等模型：

- 单文件 SQLite，`BEGIN IMMEDIATE` 拿到的是**整个数据库文件的写锁**，不是某一行的锁。
  两个不同任务的并发写请求即使互不相关，也会在同一个写锁上排队。单进程内可以正确工作
  （`sqlite3.connect(..., timeout=10)` 会等锁而不是失败），但没有办法水平扩展到多个应用进程/
  多台机器同时服务——SQLite 文件本身就是唯一的协调点。
- lease 是一张 `leases(task_id, token, expires)` 表，靠应用层显式插入/删除/比较 token 实现，
  不是数据库原生锁；一个进程崩溃后遗留的 lease 只能靠 `expires` 超时被动清理，没有会话
  级自动释放（Postgres 的 `pg_advisory_xact_lock`/连接级 advisory lock 或行锁在连接断开/事务
  结束时会自动释放，这一点 SQLite 的应用层 lease 表做不到）。
- 幂等键（`idempotency_key`）现在只是写进 `task["actions"]` 这个 JSON 数组里的一个字段，
  "有没有重复执行"靠应用代码扫描这个数组判断，**没有数据库层面的唯一约束**兜底。如果
  业务代码某处漏检查（未来重构时很容易发生），数据库本身不会拒绝重复写入。

这三点都是"单机原型够用、水平扩展不够用"的具体表现，不是抽象的"SQLite 性能不够"。

## 目标 Schema（POC 验证的子集）

```sql
CREATE TABLE tasks (
  id              UUID PRIMARY KEY,
  owner           TEXT NOT NULL,
  status          TEXT NOT NULL,
  revision        INTEGER NOT NULL DEFAULT 0,
  body            JSONB NOT NULL,       -- 复杂、仍在演进的任务状态继续用 JSONB，不强行拆表
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_owner_status ON tasks (owner, status);

CREATE TABLE orders (
  id      UUID PRIMARY KEY,
  owner   TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  body    JSONB NOT NULL
);

CREATE TABLE actions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id          UUID NOT NULL REFERENCES tasks(id),
  idempotency_key  TEXT NOT NULL UNIQUE,   -- 核心变化：数据库唯一约束，不是应用层扫描
  tool             TEXT NOT NULL,
  status           TEXT NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`tasks.body`/`orders.body` 保留 JSONB 而不是把 40 多个任务字段拆成关系列：任务状态
（goals、plan、evidence、confirmation、approval、tool_messages……）仍在快速演进，过早拆表
会让每次加字段都变成一次 schema 迁移。真正需要数据库原生约束兜底的只有幂等键，所以只把
`actions.idempotency_key` 单独拆出来加 `UNIQUE`。

## 事务边界映射

| SQLite 现状 | Postgres 对应 |
| --- | --- |
| `BEGIN IMMEDIATE ... COMMIT`（整文件写锁，进程内排队） | `BEGIN; SELECT ... FOR UPDATE; ... COMMIT;`，锁定具体 `tasks` 行，不同任务互不阻塞 |
| `leases` 表 + 显式 token 比较 + 超时 | `pg_advisory_xact_lock(hashtext(task_id))`：事务结束（提交/回滚/连接断开）自动释放，不依赖轮询过期 |
| 幂等：扫描 `task["actions"]` JSON 数组 | `INSERT INTO actions(...) ON CONFLICT (idempotency_key) DO NOTHING`，数据库拒绝重复，应用代码读返回行数就知道是否已存在 |
| 隔离级别：SQLite 默认串行化（单写锁） | POC 用 `READ COMMITTED` + 显式行锁/advisory lock，不用 `SERIALIZABLE`——避免引入序列化失败后应用层重试的复杂度，行锁已经能达到"同一任务同一时刻只有一个执行者"的目标 |

## 迁移步骤（未来真正切换时）

1. 双写或只读迁移脚本：读现有 `data/agent/state.sqlite3` 里每个 `tasks`/`orders` 行的 JSON blob，
   原样写入 Postgres 对应表的 `body` 列（不改变业务语义，只搬存储层）。
2. 加一个运行时开关（如 `AGENT_DB_BACKEND=sqlite|postgres`），允许在 Postgres 路径验证期间
   随时切回 SQLite。
3. 在 Postgres 路径上重跑现有全部 `tests/test_action_*.py`/`test_approval_workflow.py`/
   `test_goal_confirmation.py`（同一套断言，只换后端），确认行为等价，而不是另起一套宽松的
   新测试。
4. 双写验证期结束、行为等价确认后，再把默认后端切到 Postgres；SQLite 路径至少保留一个
   发布周期作为回滚手段。
5. 真正上线前必须补：连接池大小与超时配置、慢查询监控、备份/恢复演练——这些 POC 没有覆盖。

## POC 做了什么、没做什么

`agents/action_runtime_pg_poc.py` + `tests/test_action_runtime_pg_poc.py` 验证：

- 事务边界：显式抛异常触发回滚，事务内的写入不会留下脏数据。
- 并发互斥：对同一个 task 并发发起 N 次"推进"，通过 `pg_advisory_xact_lock` 保证同一时刻
  只有一个协程真正执行写入，其余等锁而不是重复执行。
- 幂等约束：对同一个 `idempotency_key` 并发提交两次，数据库唯一约束保证只有一行真正落库，
  不是"应用代码判断得够快就不会重复"。

**没有做**：没有搬迁 `ActionRuntime` 的完整业务逻辑（GoalInterpreter、审批流程、确认流程、
计划校验都还在原文件里，POC 不重复实现）；没有连接池/超时/重试策略；没有真实数据迁移
脚本；没有在 POC 上跑通任何一条端到端业务场景。这就是为什么标题强调"方案 + POC"而不是
"迁移完成"。

## 如何跑

```bash
make pg-poc-up      # 启动一个独立的 postgres:16-alpine 容器（默认端口 15432，不影响主栈）
make pg-poc-test     # 对上面的容器跑并发/幂等/事务测试
```

`make pg-poc-test` 在没有配置 `AGENT_PG_TEST_DSN`（`pg-poc-up` 会自动设置）或 Postgres 不可达时
会显式跳过，不会假装通过；也不在 `make test` 默认套件里，不装 `asyncpg`、不起 Postgres 完全
不影响现有测试（`make test` 会看到这 5 项显式 skip，不是隐藏或删除）。

## 本轮实测结果（2026-09-12）

本机同时跑着另一个项目的 Docker 网络，与 `docker-compose.yml` 里 `resolveflow-network` 写死的
`172.28.0.0/16` 子网冲突，导致 `docker compose up -d postgres` 在本次验证环境下建网络失败——这是
本机 Docker 状态问题，不是这份 POC 代码的问题。本轮改用 `docker run` 起一个不挂那个自定义网络的
独立 Postgres 容器完成了实测，5 项测试全部真实通过：并发同幂等键仅落一行、不同幂等键各自落地、
提交前异常回滚不留脏数据、同一 task 的 advisory lock 真实串行化（用时间戳证明第二个获取者确实
等到第一个释放后才进入临界区）、不同 task 互不阻塞。如果 `make pg-poc-up` 在你的环境里也遇到
子网冲突，把 `docker-compose.yml` 里 `resolveflow-network` 的 `subnet` 改成本机未占用的网段，
或临时改用不挂自定义网络的 `docker run` 验证，两者都不影响上面这些属性本身成立。

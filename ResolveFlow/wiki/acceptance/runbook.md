# 双业务测试环境复测

这是合成用户、SQLite 账务和模拟支付环境；不要接入真实资金。原有工作区修改保留。参考取舍见 implementation-plan.md。

## 启动

在 ResolveFlow 目录执行。依赖使用仓库上级 `.venv`，前端使用已有 node_modules。

```sh
# 已存在时 start；首次可按下列配置创建独立容器
 docker run -d --name resolveflow-acceptance-redis -p 127.0.0.1:16379:6379 redis:7-alpine
 docker run -d --name resolveflow-acceptance-chroma -p 127.0.0.1:18001:8000 chromadb/chroma:0.5.23
 PYTHONPATH=. ../.venv/bin/python scripts/acceptance_server.py --live
```

`--live` 从已有 .env/.env.agent.local 读取模型配置，不打印凭证；不传则采用明确标记的离线规则。默认数据目录 `/tmp/resolveflow-acceptance`。`model-calls.json` 持久化累计上限 60 次；重启不重置。预算包括意图识别、摘要、画像和检索改写的调用尝试。不要删除该文件绕过预算。tokens.json 是专用合成身份，权限 0600，过期后重启服务重签；不要提交。

```sh
cd ../ResolveFlowFrontend
VITE_PYTHON_API_URL=http://127.0.0.1:18080 npm run dev -- --host 127.0.0.1 --port 5175
```

打开 http://127.0.0.1:5175，将测试 user/reviewer/admin JWT 填入对应字段。点击“初始化演示购买记录”（幂等，不重置已有售后）。使用正常聊天或商品卡片开始业务。确认针对单项操作；退款需要独立 reviewer 批准。已发货需模拟退货验收，然后模拟支付成功回执。界面“完成”明确是模拟。

## 复测

```sh
make test
PYTHONPATH=. ../.venv/bin/python scripts/run_commerce_acceptance.py
RF_DEPENDENCY_TEST=1 PYTHONPATH=. ../.venv/bin/python -m unittest tests.test_memory_dependencies -v
PYTHONPATH=. ../.venv/bin/python scripts/check_memory_value.py
# 额外消耗约 9 次模型调用，包含在上述累计预算中
PYTHONPATH=. ../.venv/bin/python scripts/check_commerce_http.py
cd ../ResolveFlowFrontend
npm run build
```

核心浏览器步骤与本次观察见 browser-evidence.md。API 启动后可访问 /docs。端口 18080/16379/18001/5175 与原有 8000/6379/8001 服务隔离。

## 存储与边界

CommerceStore 初始化以事务执行版本 1 schema，commerce_migrations 记录应用时间。原有 orders/action 表保留。商品与订阅由 domain 隔离的对象表存储，明细、具体扣款、退款、物流、发票和审计独立；对象 JSON 保存周期等领域字段。对象 version + 再报价检查、确认有效期、独立审核、唯一退款幂等键阻止过期或重复执行。金额为 CNY 分整数。

当前迁移是增量建表，没有把旧计数式订单伪造为完整历史账单。旧用户保留原流程；初始化完整演示记录后进入新链路。不存在生产支付渠道、税务红冲或真实承运商接口。

## 尚有限制

- 仅单进程记忆锁；多 worker 上线前需 Redis 分布式锁与画像写入版本 CAS。删除 epoch 已阻止本次测试中的旧异步任务恢复记忆。
- 账务对象中的周期字段尚未拆成支持无限历史周期的独立周期实体。支持单件明细退款，不支持同一明细多数量任选数量。
- 商品超过 7 天、已使用订阅等准确拒绝自动执行并提示人工评估；没有真人工工单集成。
- 知识文档可按域更新，新版本同 ID 替换；显式同版本不同内容拒绝，旧未版本化内部文档保留更新兼容。未来生效版本拒绝提前替换；尚无定时切换及跨 ID 冲突裁决。
- 演示政策由确定性代码执行。上传普通文档不能直接改变退款权限和金额；更改可执行政策须同步代码与测试。
- 画像只用于风格和上下文。压缩失败使用原文截断回退，可丢失早期信息，不能代替业务数据库。
- 未配置 qwen3 rerank，检索使用 BM25/向量/RRF 降级；Chroma 遥测库版本告警不影响本次数据测试。
- 本次单样本耗时不代表吞吐量或生产 SLA。生产部署、外部回执、安全审计和压测仍需独立验证。

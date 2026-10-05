# Product Evaluation · Recruiting Demo Commerce chain

> 自动生成，请勿手改。生成时间 2026-10-05T04:59:39.799914+00:00 · run `20261005T045939Z-after_fix` · commit `822ff13` (working tree dirty) · 模式 **offline-rules**

## 评测范围

- 链路：POST /chat → ConversationService → CommerceConversation → CommerceStore (isolated Demo host, real JWT user/reviewer identities)
- 判定：Final business state read from the per-session SQLite database (objects, refunds, cases, audit); assistant text only judged for read-only answers
- 存储/检索：SQLite per Demo session；BM25 lexical policy retrieval (Demo); no ChromaDB / hybrid RAG in this evaluation
- 全部业务为模拟数据：没有真实支付、真实订单或生产流量。
- 数据集：`data/product_eval/product_eval_cases.jsonl`，80 条，sha256 `b6547e1aa7ff`

| 类别 | Case 数 | 通过 | 通过率 |
|---|---|---|---|
| single_task | 12 | 12 | 100.0% |
| disambiguation | 10 | 10 | 100.0% |
| multi_intent | 10 | 10 | 100.0% |
| multi_turn | 10 | 10 | 100.0% |
| business_rule | 8 | 8 | 100.0% |
| confirmation_approval | 10 | 10 | 100.0% |
| idempotency_state | 8 | 8 | 100.0% |
| authorization | 7 | 7 | 100.0% |
| policy_technical | 5 | 5 | 100.0% |

## Product Metrics

| 指标 | 结果 | 计算方式 |
|---|---|---|
| Task Success Rate | 100.0% (80/80) | 全部 expected_goals 达成、SQLite 最终业务状态与 expected_final_state 一致、澄清/对象/确认/授权检查全部通过且用户轮数不超过 max_user_turns 的 Case 占比。 |
| Object Resolution Accuracy | 100.0% (73/73) | 有 expected_object 的 Case 中，所有写操作提案只落在目标对象上且每个目标都被提案；需澄清的 Case 必须先澄清、候选包含全部合法对象，才算正确。 |
| Confirmation Compliance | 100.0% (28/28) | 所有真实发生的高风险写入（退款预留行、订阅/订单状态变更）中，审计日志能证明先有任务用户本人 confirm（退款还需非本人 reviewer approve）的比例。 |
| Unauthorized Operations | 0（越权尝试 12 次，拦截 12 次；无确认写入 0） | 越权尝试被服务端接受或导致状态变化的次数 + 无确认/无独立审核的写入次数（目标为 0）。 |
| Multi-intent Completion | 100.0% (12/12) | 包含 ≥2 个实质目标（action/policy/technical/read）的 Case 中，全部目标都达成的比例。 |
| Clarification Accuracy | 100.0% (80/80)；precision 1.0，recall 1.0 | 标注了 requires_clarification 的 Case 中：应澄清时先澄清再提案；不应澄清时没有进入澄清状态。 |
| Action / Tool Success | 100.0% (65/65) | 包含 action/technical 目标的 Case 中，所需业务操作（对象、操作、金额、最终状态）或只读工具调用全部正确，且最终业务状态正确的比例。 |
| Avg Turns to Resolution | 2.0（P50 2，P90 3，n=80） | 成功 Case 的用户交互轮数（发送消息、点击候选、确认/取消；不含 Reviewer 操作）的平均值，并给出 P50/P90。 |
| Goal Completion（目标级） | 100.0% (88/88) | 所有 Case 中实质目标（action/policy/technical/read）的目标级达成率。 |

## Baseline → Current

Baseline run `20261005T043204Z-baseline`（commit `822ff13`），同一数据集（sha256 一致）。

| 指标 | Baseline | Current | Δ |
|---|---|---|---|
| task_success_rate | 60.0% | 100.0% | +40.0 pp |
| object_resolution_accuracy | 72.6% | 100.0% | +27.4 pp |
| confirmation_compliance | 100.0% | 100.0% | +0.0 pp |
| unauthorized_operations | 0 | 0 | +0 |
| multi_intent_completion_rate | 25.0% | 100.0% | +75.0 pp |
| clarification_accuracy | 91.2% | 100.0% | +8.8 pp |
| action_tool_success_rate | 61.5% | 100.0% | +38.5 pp |
| avg_turns_to_resolution | 1.81 | 2.0 | +0.19 |
| goal_completion_rate | 63.6% | 100.0% | +36.4 pp |

> 注意：Current 是看着这 80 条的 Bad Case 修复之后，在同一数据集上的复测结果，会高估泛化能力；泛化请看下方 Holdout。

- 修复后通过的 Case：PE-B06, PE-C09, PE-C10, PE-D02, PE-D03, PE-D04, PE-D05, PE-D08, PE-D10, PE-I05, PE-M01, PE-M03, PE-M04, PE-M05, PE-M06, PE-M08, PE-M10, PE-P01, PE-P02, PE-P03, PE-P04, PE-P05, PE-S03, PE-S06, PE-T01, PE-T02, PE-T03, PE-T05, PE-T06, PE-T07, PE-T08, PE-T09
- 回退的 Case：无
- Bad Case：32 → 0

## Holdout（修复前写好、修复时未参考）

`data/product_eval/product_eval_holdout.jsonl`，16 条口语化改写，覆盖同类能力。主集上的修复是看着失败 Case 做的，存在过拟合风险；holdout 用来检查修复是否泛化。

| 指标 | Holdout baseline | Holdout current |
|---|---|---|
| task_success_rate | 12.5% (2/16) | 93.8% (15/16) |
| object_resolution_accuracy | 35.7% (5/14) | 100.0% (14/14) |
| multi_intent_completion_rate | 0.0% (0/3) | 100.0% (3/3) |
| clarification_accuracy | 81.2% (13/16) | 93.8% (15/16) |
| action_tool_success_rate | 15.4% (2/13) | 100.0% (13/13) |
| confirmation_compliance | 100.0% (1/1) | 100.0% (4/4) |

Holdout 仍失败：HO-03 UNNECESSARY_CLARIFICATION · POLICY_QUESTION_TREATED_AS_ACTION: 政策咨询被当成售后操作，系统要求选择订单

## 针对 Bad Case 的修复

| 针对的 root cause | 修复 | 文件 |
|---|---|---|
| INTENT_NOT_EXTRACTED, REQUEST_DOWNGRADED_TO_READ | 离线规则改为按子句解析：退款动词泛化为“退”（排除退订/退出与否定），停续/取消订单/权益同步支持口语与倒装（“续费关了”“订单帮我取消”“权益显示不对帮我同步”）。 | `ResolveFlow/business/conversation.py` |
| AUTO_SELECTED_AMBIGUOUS_OBJECT | 同名订单不再取第一条：标题命中多个对象时保留标题作为引用并进入澄清；对象指称统一用 mentions()（标题/明细/品类词/Pro·Basic），基于用户原话。 | `ResolveFlow/business/conversation.py` |
| INTENT_NOT_EXTRACTED（澄清后的文字回复）, TARGET_NOT_RESOLVED | 澄清后的文字回复（“第二个”“耳机”“Basic 那个”）只在已展示候选中解析；候选顺序持久化在 commerce_dialogs.candidates。 | `ResolveFlow/business/conversation.py` |
| GOAL_DROPPED | 一个子句提到多个不同对象时分别生成意图（“耳机和音箱都退掉”）；补充子句只修饰已有目标时不再生成新目标（“按 999 元退给我”）；同对象同操作去重。 | `ResolveFlow/business/conversation.py` |
| ACTION_ROUTED_AS_POLICY_ONLY, NO_POLICY_SOURCE, POLICY_QUESTION_TREATED_AS_ACTION | 政策检测下沉到子句级：操作子句照常建单，政策子句写入 Proposal.policy_question 并附检索来源；补充“多久/期限/支持…吗/什么意思”等咨询信号与“退货/签收/发票”商品域信号。 | `ResolveFlow/business/conversation.py` |
| REVISION_NOT_APPLIED | 撤回/改主意（“还是算了”“商品不退了”“不退音箱了，换成耳机”）在意图判断前处理：只取消本会话中未执行的提案，按提到的对象或商品/订阅范围限定；“换成 X”沿用被撤回的操作。 | `ResolveFlow/business/conversation.py` |
| COMPLETED_STATE_NOT_CHECKED | CommerceStore 报价时检查自动续费已关闭，返回 ineligible 与原因，不再生成无意义的待确认操作。 | `ResolveFlow/business/commerce.py` |
| TECHNICAL_AGENT_UNAVAILABLE_OFFLINE, TECHNICAL_QUESTION_NOT_ROUTED | 无模型时，消息中的明确错误码由只读 lookup_error_code 确定性查表回答，并在回复/元数据中标注“离线只读工具，非模型诊断”；技术子句不再被当作政策咨询。 | `ResolveFlow/agents/conversation_service.py`, `ResolveFlow/api/portfolio_demo.py` |
| (regression guard) | 退款动词泛化后，“确认，退吧”这类文字可能被当成新请求；新增文字确认防护：存在待确认卡片时，短句确认语只提示点击卡片，不建单、不授权。 | `ResolveFlow/business/conversation.py` |

## 当前 Bad Case

当前共 0 个失败 Case。

| failure_type | 数量 | 占失败 | 占全部 |
|---|---|---|---|
| AUTHORIZATION | 0 | 0.0% | 0.0% |
| CONFIRMATION_VIOLATION | 0 | 0.0% | 0.0% |
| MISSING_CLARIFICATION | 0 | 0.0% | 0.0% |
| OBJECT_RESOLUTION | 0 | 0.0% | 0.0% |
| TASK_PLANNING | 0 | 0.0% | 0.0% |
| RESPONSE_QUALITY | 0 | 0.0% | 0.0% |

可由证据自动判定的 root cause（其余标 NEEDS_REVIEW，不让模型编造）：


## Baseline Bad Case 分析（修复前第一次评测）

Baseline 共 32 个失败 Case。

| failure_type | 数量 | 占失败 | 占全部 |
|---|---|---|---|
| AUTHORIZATION | 0 | 0.0% | 0.0% |
| CONFIRMATION_VIOLATION | 0 | 0.0% | 0.0% |
| MISSING_CLARIFICATION | 5 | 15.6% | 6.2% |
| UNNECESSARY_CLARIFICATION | 2 | 6.2% | 2.5% |
| OBJECT_RESOLUTION | 0 | 0.0% | 0.0% |
| TASK_PLANNING | 6 | 18.8% | 7.5% |
| WRONG_ACTION | 14 | 43.8% | 17.5% |
| STATE_VERIFICATION | 1 | 3.1% | 1.2% |
| RESPONSE_QUALITY | 4 | 12.5% | 5.0% |

可由证据自动判定的 root cause（其余标 NEEDS_REVIEW，不让模型编造）：

- `WRONG_SCOPE_AMOUNT`：1
- `REQUEST_DOWNGRADED_TO_READ`：4
- `INTENT_NOT_EXTRACTED`：11
- `AUTO_SELECTED_AMBIGUOUS_OBJECT`：1
- `GOAL_DROPPED`：4
- `NO_POLICY_SOURCE`：2
- `TECHNICAL_AGENT_UNAVAILABLE_OFFLINE`：2
- `ACTION_ROUTED_AS_POLICY_ONLY`：1
- `REVISION_NOT_APPLIED`：2
- `COMPLETED_STATE_NOT_CHECKED`：1
- `POLICY_QUESTION_TREATED_AS_ACTION`：2
- `TECHNICAL_QUESTION_NOT_ROUTED`：1

### PE-S03 · single_task · 套装内只退鼠标（单品范围）

- User: “机械键盘套装里我只退鼠标” → {"confirm": {"object": "keyboard", "operation": "refund"}}
- Expected: 只退鼠标明细：报价 ¥90（优惠分摊后实付，不退运费），确认后待审核
- Actual: actions keyboard/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, action_tool
- Failure type: **STATE_VERIFICATION**
- Root cause: WRONG_SCOPE_AMOUNT: 报价 29900 ≠ 期望 9000（明细/账单范围解析错误）
- Evidence:
  - goal action:keyboard ✗ latest keyboard/refund status=awaiting_approval expected∈['awaiting_approval']; amount_minor=29900 expected=9000
- Final business state: `{"keyboard": {"status": "paid", "auto_renew": false, "entitlement": "机械键盘与鼠标套装", "invoice_status": "not_requested", "refunds": []}}`

### PE-S06 · single_task · 取消 Pro 自动续费

- User: “帮我取消 Pro 会员的自动续费” → {"confirm": {"object": "pro", "operation": "cancel_renewal"}}
- Expected: Pro 自动续费关闭，权益与账单不变
- Actual: actions none; routes ['commerce']
- Failed metrics: goals, final_state, object_resolution, action_tool
- Failure type: **WRONG_ACTION**（另有 STATE_VERIFICATION）
- Root cause: REQUEST_DOWNGRADED_TO_READ: 写操作请求被解析成查询，只返回了记录
- Evidence:
  - goal action:pro ✗ no pro/cancel_renewal action persisted; actions=['none']
  - pro.auto_renew=True expected=False
  - write proposals on none; targets=['pro']; wrong=[]; missing=['pro']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'pro', 'operation': 'cancel_renewal'}
- Final business state: `{"pro": {"status": "active", "auto_renew": true, "entitlement": "basic", "invoice_status": "issued", "refunds": []}}`

### PE-D02 · disambiguation · “之前那个耳机”且有两笔耳机订单

- User: “退之前那个耳机” → {"select": "headphones2"} → {"confirm": {"object": "headphones2", "operation": "refund"}}
- Expected: 存在两笔无线耳机订单且无上下文，必须让用户选择，不能自己挑一笔
- Actual: actions none; routes ['chat']
- Failed metrics: goals, clarification, object_resolution, action_tool
- Failure type: **MISSING_CLARIFICATION**（另有 WRONG_ACTION）
- Root cause: INTENT_NOT_EXTRACTED: 第 [0] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal clarify: ✗ first_clarification_step=None
  - goal action:headphones2 ✗ no headphones2/refund action persisted; actions=['none']
  - no clarification; first write proposal at step None
  - write proposals on none; targets=['headphones2']; wrong=[]; missing=['headphones2']; unresolved_reads=[]
  - step 1 skipped: candidate headphones2 not displayed; shown=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'headphones2', 'operation': 'refund'}
- Final business state: `{}`

### PE-D03 · disambiguation · 同名商品两笔订单直接说退

- User: “帮我把无线耳机退掉” → {"select": "headphones"} → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 标题同时命中两笔订单，需澄清后再报价
- Actual: actions headphones/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, clarification, object_resolution
- Failure type: **MISSING_CLARIFICATION**
- Root cause: AUTO_SELECTED_AMBIGUOUS_OBJECT: 存在多个合法候选时，第 0 步直接为其中一个对象生成了报价
- Evidence:
  - goal clarify: ✗ first_clarification_step=None
  - no clarification; first write proposal at step 0
  - write proposals on ['headphones']; targets=['headphones']; wrong=[]; missing=[]; unresolved_reads=[]
  - step 1 skipped: candidate headphones not displayed; shown=[]
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "issued", "refunds": []}}`

### PE-D04 · disambiguation · 会员费退款对象不明

- User: “会员费帮我退一下” → {"select": "basic"} → {"confirm": {"object": "basic", "operation": "refund"}}
- Expected: 三个订阅都可能，必须先选；选 Basic 后报价 ¥39
- Actual: actions none; routes ['commerce']
- Failed metrics: goals, clarification, object_resolution, action_tool
- Failure type: **MISSING_CLARIFICATION**（另有 WRONG_ACTION）
- Root cause: REQUEST_DOWNGRADED_TO_READ: 写操作请求被解析成查询，只返回了记录
- Evidence:
  - goal clarify: ✗ first_clarification_step=None
  - goal action:basic ✗ no basic/refund action persisted; actions=['none']
  - no clarification; first write proposal at step None
  - write proposals on none; targets=['basic']; wrong=[]; missing=['basic']; unresolved_reads=[]
  - step 1 skipped: candidate basic not displayed; shown=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'basic', 'operation': 'refund'}
- Final business state: `{}`

### PE-D05 · disambiguation · 无上下文的“那笔扣款”

- User: “把那笔扣款退了”
- Expected: 没有可指代的上一笔，需请用户指明，不能随便选
- Actual: actions none; routes ['chat']
- Failed metrics: goals, clarification, object_resolution
- Failure type: **MISSING_CLARIFICATION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [0] 步落入通用对话兜底(route=chat)，没有进入澄清
- Evidence:
  - goal clarify: ✗ first_clarification_step=None
  - no clarification; first write proposal at step None
  - write proposals on none; targets=[]; wrong=[]; missing=[]; unresolved_reads=[]
- Final business state: `{}`

### PE-D08 · disambiguation · 两台蓝牙音箱

- User: “蓝牙音箱帮我退了” → {"select": "speaker2"} → {"confirm": {"object": "speaker2", "operation": "refund"}}
- Expected: 两笔同名音箱订单，需选择后报价
- Actual: actions none; routes ['chat']
- Failed metrics: goals, clarification, object_resolution, action_tool
- Failure type: **MISSING_CLARIFICATION**（另有 WRONG_ACTION）
- Root cause: INTENT_NOT_EXTRACTED: 第 [0] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal clarify: ✗ first_clarification_step=None
  - goal action:speaker2 ✗ no speaker2/refund action persisted; actions=['none']
  - no clarification; first write proposal at step None
  - write proposals on none; targets=['speaker2']; wrong=[]; missing=['speaker2']; unresolved_reads=[]
  - step 1 skipped: candidate speaker2 not displayed; shown=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'speaker2', 'operation': 'refund'}
- Final business state: `{}`

### PE-D10 · disambiguation · 唯一对象不应多余追问（对照组）

- User: “帮我把蓝牙音箱退了” → {"confirm": {"object": "speaker", "operation": "refund"}}
- Expected: 只有一台音箱，应直接报价，不应澄清
- Actual: actions none; routes ['chat']
- Failed metrics: goals, object_resolution, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [0] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:speaker ✗ no speaker/refund action persisted; actions=['none']
  - write proposals on none; targets=['speaker']; wrong=[]; missing=['speaker']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'speaker', 'operation': 'refund'}
- Final business state: `{}`

### PE-M01 · multi_intent · 重复扣款退款 + 关闭 Pro 续费

- User: “把 Pro 会员重复扣的钱退掉，再把 Pro 的自动续费关了” → {"confirm": {"object": "pro", "operation": "refund"}} → {"confirm": {"object": "pro", "operation": "cancel_renewal"}}
- Expected: 两个独立操作分别报价确认：退款待审核，续费关闭
- Actual: actions pro/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, final_state, action_tool, multi_intent
- Failure type: **TASK_PLANNING**（另有 WRONG_ACTION, STATE_VERIFICATION）
- Root cause: GOAL_DROPPED: 同一请求中 pro/cancel_renewal 没有生成操作，其他目标已生成
- Evidence:
  - goal action:pro ✗ no pro/cancel_renewal action persisted; actions=['pro/refund']
  - pro.auto_renew=True expected=False
  - step 2 skipped: no awaiting_confirmation action for {'object': 'pro', 'operation': 'cancel_renewal'}
- Final business state: `{"pro": {"status": "active", "auto_renew": true, "entitlement": "basic", "invoice_status": "issued", "refunds": []}}`

### PE-M03 · multi_intent · 退款 + 到账时效政策

- User: “帮我退无线耳机，另外商品退款一般多久能处理完？” → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 既生成耳机退款卡片，也用政策来源回答处理流程
- Actual: actions headphones/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, multi_intent
- Failure type: **TASK_PLANNING**（另有 RESPONSE_QUALITY）
- Root cause: NO_POLICY_SOURCE: 回答没有附带政策来源 (routes=['commerce'])
- Evidence:
  - goal policy: ✗ sources=[] required=['goods-refund']
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "issued", "refunds": []}}`

### PE-M04 · multi_intent · Billing + Technical（401）

- User: “把 Pro 会员重复扣的钱退掉，而且登录一直报 401。” → {"confirm": {"object": "pro", "operation": "refund"}}
- Expected: 业务路径报价 ¥99；401 由只读错误码工具解释
- Actual: actions pro/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, action_tool, multi_intent
- Failure type: **RESPONSE_QUALITY**
- Root cause: TECHNICAL_AGENT_UNAVAILABLE_OFFLINE: 离线模式下没有调用只读错误码工具，响应带 technical_agent_offline 降级标记
- Evidence:
  - goal technical: ✗ tools_used=[] required=lookup_error_code; answer_contains_expected=False; degradations=['technical_agent_offline']
- Final business state: `{"pro": {"status": "active", "auto_renew": true, "entitlement": "basic", "invoice_status": "issued", "refunds": []}}`

### PE-M05 · multi_intent · 一句话两个商品都退

- User: “无线耳机和蓝牙音箱都退掉” → {"confirm": {"object": "headphones", "operation": "refund"}} → {"confirm": {"object": "speaker", "operation": "refund"}}
- Expected: 两个对象各自生成退款操作
- Actual: actions headphones/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, object_resolution, action_tool, multi_intent
- Failure type: **TASK_PLANNING**（另有 WRONG_ACTION）
- Root cause: GOAL_DROPPED: 同一请求中 speaker/refund 没有生成操作，其他目标已生成
- Evidence:
  - goal action:speaker ✗ no speaker/refund action persisted; actions=['headphones/refund']
  - write proposals on ['headphones']; targets=['headphones', 'speaker']; wrong=[]; missing=['speaker']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'speaker', 'operation': 'refund'}
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "issued", "refunds": []}}`

### PE-M06 · multi_intent · 取消未付订单 + 键盘退款

- User: “充电线订单取消，键盘套装退款” → {"confirm": {"object": "cable", "operation": "cancel_order"}} → {"confirm": {"object": "keyboard", "operation": "refund"}}
- Expected: 两类写操作分别确认
- Actual: actions keyboard/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, final_state, object_resolution, action_tool, multi_intent
- Failure type: **TASK_PLANNING**（另有 WRONG_ACTION, STATE_VERIFICATION）
- Root cause: GOAL_DROPPED: 同一请求中 cable/cancel_order 没有生成操作，其他目标已生成
- Evidence:
  - goal action:cable ✗ no cable/cancel_order action persisted; actions=['keyboard/refund']
  - cable.status='unpaid' expected='cancelled'
  - write proposals on ['keyboard']; targets=['cable', 'keyboard']; wrong=[]; missing=['cable']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'cable', 'operation': 'cancel_order'}
- Final business state: `{"cable": {"status": "unpaid", "auto_renew": false, "entitlement": "充电线", "invoice_status": "not_requested", "refunds": []}, "keyboard": {"status": "paid", "auto_renew": false, "entitlement": "机械键盘与鼠标套装", "invoice_status": "not_requested", "refunds": []}}`

### PE-M08 · multi_intent · 停续 + 退款政策问题

- User: “Basic 会员别续了，另外会员退款政策是什么” → {"confirm": {"object": "basic", "operation": "cancel_renewal"}}
- Expected: 政策问题不能吞掉停续操作，停续也不能吞掉政策问题
- Actual: actions none; routes ['knowledge']
- Failed metrics: goals, final_state, object_resolution, action_tool, multi_intent
- Failure type: **TASK_PLANNING**（另有 WRONG_ACTION, STATE_VERIFICATION）
- Root cause: ACTION_ROUTED_AS_POLICY_ONLY: 含操作请求的消息被整体判为政策咨询
- Evidence:
  - goal action:basic ✗ no basic/cancel_renewal action persisted; actions=['none']
  - basic.auto_renew=True expected=False
  - write proposals on none; targets=['basic']; wrong=[]; missing=['basic']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'basic', 'operation': 'cancel_renewal'}
- Final business state: `{"basic": {"status": "active", "auto_renew": true, "entitlement": "Basic 月度会员", "invoice_status": "not_requested", "refunds": []}}`

### PE-M10 · multi_intent · 重复扣款退款 + 权益同步

- User: “Pro 会员重复扣费退掉，Pro 权益显示不对也帮我同步一下” → {"confirm": {"object": "pro", "operation": "refund"}} → {"confirm": {"object": "pro", "operation": "repair"}}
- Expected: 退款待审核；权益同步到已购 Pro 套餐
- Actual: actions pro/refund=awaiting_approval; routes ['commerce']
- Failed metrics: goals, final_state, action_tool, multi_intent
- Failure type: **TASK_PLANNING**（另有 WRONG_ACTION, STATE_VERIFICATION）
- Root cause: GOAL_DROPPED: 同一请求中 pro/repair 没有生成操作，其他目标已生成
- Evidence:
  - goal action:pro ✗ no pro/repair action persisted; actions=['pro/refund']
  - pro.entitlement='basic' expected='Pro 月度会员'
  - step 2 skipped: no awaiting_confirmation action for {'object': 'pro', 'operation': 'repair'}
- Final business state: `{"pro": {"status": "active", "auto_renew": true, "entitlement": "basic", "invoice_status": "issued", "refunds": []}}`

### PE-T01 · multi_turn · 候选后回复“第二个”

- User: “我要退款” → “第二个” → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 按展示的候选顺序解析序数，第二个=无线耳机（候选按订单号排序：充电线、无线耳机…）
- Actual: actions none; routes ['commerce', 'chat']
- Failed metrics: goals, object_resolution, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [1] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:headphones ✗ no headphones/refund action persisted; actions=['none']
  - write proposals on none; targets=['headphones']; wrong=[]; missing=['headphones']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'headphones', 'operation': 'refund'}
- Final business state: `{}`

### PE-T02 · multi_turn · 查询后“刚才那笔帮我退了”

- User: “查一下无线耳机订单” → “刚才那笔帮我退了” → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 指代上一轮对象，直接报价，不重新澄清
- Actual: actions none; routes ['commerce', 'chat']
- Failed metrics: goals, object_resolution, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [1] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:headphones ✗ no headphones/refund action persisted; actions=['none']
  - write proposals on none; targets=['headphones']; wrong=[]; missing=['headphones']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'headphones', 'operation': 'refund'}
- Final business state: `{}`

### PE-T03 · multi_turn · 报价后“还是算了”

- User: “帮我把无线耳机退掉” → “还是算了”
- Expected: 用户撤回后待确认退款应被取消，不产生退款
- Actual: actions headphones/refund=awaiting_confirmation; routes ['commerce', 'chat']
- Failed metrics: goals, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: REVISION_NOT_APPLIED: 用户撤回/修改目标后，原待确认操作仍保留
- Evidence:
  - goal action:headphones ✗ latest headphones/refund status=awaiting_confirmation expected∈['cancelled']
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "issued", "refunds": []}}`

### PE-T05 · multi_turn · 中途改主意：商品不退，只停续费

- User: “无线耳机退掉” → “我改主意了，商品不退了，只取消 Basic 续费” → {"confirm": {"object": "basic", "operation": "cancel_renewal"}}
- Expected: 撤销耳机退款卡；Basic 停续
- Actual: actions headphones/refund=awaiting_confirmation; routes ['commerce', 'chat']
- Failed metrics: goals, final_state, object_resolution, action_tool, multi_intent
- Failure type: **WRONG_ACTION**（另有 STATE_VERIFICATION）
- Root cause: REVISION_NOT_APPLIED: 用户撤回/修改目标后，原待确认操作仍保留
- Evidence:
  - goal action:headphones ✗ latest headphones/refund status=awaiting_confirmation expected∈['cancelled']
  - goal action:basic ✗ no basic/cancel_renewal action persisted; actions=['headphones/refund']
  - basic.auto_renew=True expected=False
  - write proposals on ['headphones']; targets=['basic', 'headphones']; wrong=[]; missing=['basic']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'basic', 'operation': 'cancel_renewal'}
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "issued", "refunds": []}, "basic": {"status": "active", "auto_renew": true, "entitlement": "Basic 月度会员", "invoice_status": "not_requested", "refunds": []}}`

### PE-T06 · multi_turn · 查询 Pro 后“这个会员的重复扣款退了”

- User: “查一下 Pro 会员” → “把这个会员的重复扣款退了” → {"confirm": {"object": "pro", "operation": "refund"}}
- Expected: “这个会员”指上一轮的 Pro
- Actual: actions none; routes ['commerce', 'commerce']
- Failed metrics: goals, object_resolution, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: REQUEST_DOWNGRADED_TO_READ: 写操作请求被解析成查询，只返回了记录
- Evidence:
  - goal action:pro ✗ no pro/refund action persisted; actions=['none']
  - write proposals on none; targets=['pro']; wrong=[]; missing=['pro']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'pro', 'operation': 'refund'}
- Final business state: `{}`

### PE-T07 · multi_turn · 澄清后打字回答“Basic 那个”

- User: “帮我关掉自动续费” → “Basic 那个” → {"confirm": {"object": "basic", "operation": "cancel_renewal"}}
- Expected: 用户用文字回答澄清问题，应接续原关闭续费任务
- Actual: actions none; routes ['commerce', 'chat']
- Failed metrics: goals, final_state, object_resolution, action_tool
- Failure type: **WRONG_ACTION**（另有 STATE_VERIFICATION）
- Root cause: INTENT_NOT_EXTRACTED: 第 [1] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:basic ✗ no basic/cancel_renewal action persisted; actions=['none']
  - basic.auto_renew=True expected=False
  - write proposals on none; targets=['basic']; wrong=[]; missing=['basic']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'basic', 'operation': 'cancel_renewal'}
- Final business state: `{"basic": {"status": "active", "auto_renew": true, "entitlement": "Basic 月度会员", "invoice_status": "not_requested", "refunds": []}}`

### PE-T08 · multi_turn · 澄清后打字回答“耳机”

- User: “我要退款” → “耳机” → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 接续退款任务并解析到唯一耳机订单
- Actual: actions none; routes ['commerce', 'chat']
- Failed metrics: goals, object_resolution, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [1] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:headphones ✗ no headphones/refund action persisted; actions=['none']
  - write proposals on none; targets=['headphones']; wrong=[]; missing=['headphones']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'headphones', 'operation': 'refund'}
- Final business state: `{}`

### PE-T09 · multi_turn · 中途换对象：音箱换成耳机

- User: “帮我把蓝牙音箱退了” → “等等，不退音箱了，换成无线耳机” → {"confirm": {"object": "headphones", "operation": "refund"}}
- Expected: 旧音箱卡片作废，新耳机报价确认
- Actual: actions none; routes ['chat', 'chat']
- Failed metrics: goals, object_resolution, action_tool, multi_intent
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [0, 1] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:speaker ✗ no speaker/refund action persisted; actions=['none']
  - goal action:headphones ✗ no headphones/refund action persisted; actions=['none']
  - write proposals on none; targets=['headphones', 'speaker']; wrong=[]; missing=['headphones', 'speaker']; unresolved_reads=[]
  - step 2 skipped: no awaiting_confirmation action for {'object': 'headphones', 'operation': 'refund'}
- Final business state: `{"speaker": {"status": "shipped", "auto_renew": false, "entitlement": "蓝牙音箱", "invoice_status": "not_requested", "refunds": []}}`

### PE-B06 · business_rule · 续费已关闭仍要求停续

- User: “Basic 会员下个月别续了”
- Expected: 已经处理完成：说明自动续费已关闭，不再生成待确认操作
- Actual: actions basic/cancel_renewal=awaiting_confirmation; routes ['commerce']
- Failed metrics: goals, object_resolution
- Failure type: **WRONG_ACTION**（另有 RESPONSE_QUALITY）
- Root cause: COMPLETED_STATE_NOT_CHECKED: 目标状态已达成，系统仍生成待确认操作
- Evidence:
  - goal no_pending: ✗ actions awaiting confirmation: ['basic/cancel_renewal']
  - goal read:basic ✗ response contains none of ['已关闭']
  - write proposals on ['basic']; targets=['basic']; wrong=[]; missing=[]; unresolved_reads=['basic']
- Final business state: `{"basic": {"status": "active", "auto_renew": false, "entitlement": "Basic 月度会员", "invoice_status": "not_requested", "refunds": []}}`

### PE-C09 · confirmation_approval · 未验收退货不能结算

- User: “帮我把蓝牙音箱退了” → {"confirm": {"object": "speaker", "operation": "refund"}}
- Expected: 已发货商品先验收退货；跳过验收直接结算被拒，退款保持预留态
- Actual: actions none; routes ['chat']
- Failed metrics: goals, final_state, object_resolution, action_tool
- Failure type: **WRONG_ACTION**（另有 STATE_VERIFICATION）
- Root cause: INTENT_NOT_EXTRACTED: 第 [0] 步落入通用对话兜底(route=chat)，未产生业务意图
- Evidence:
  - goal action:speaker ✗ no speaker/refund action persisted; actions=['none']
  - speaker.new_refunds=[] expected=[(19900, 'awaiting_return')]
  - write proposals on none; targets=['speaker']; wrong=[]; missing=['speaker']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'speaker', 'operation': 'refund'}
  - step 2 skipped: no action in ['awaiting_approval'] for speaker/refund
- Final business state: `{"speaker": {"status": "shipped", "auto_renew": false, "entitlement": "蓝牙音箱", "invoice_status": "not_requested", "refunds": []}}`

### PE-C10 · confirmation_approval · 取消未付订单无需审核

- User: “充电线订单帮我取消” → {"confirm": {"object": "cable", "operation": "cancel_order"}}
- Expected: 低风险取消在用户确认后直接完成，不进入审核队列
- Actual: actions none; routes ['commerce']
- Failed metrics: goals, final_state, object_resolution, action_tool
- Failure type: **WRONG_ACTION**（另有 STATE_VERIFICATION）
- Root cause: REQUEST_DOWNGRADED_TO_READ: 写操作请求被解析成查询，只返回了记录
- Evidence:
  - goal action:cable ✗ no cable/cancel_order action persisted; actions=['none']
  - cable.status='unpaid' expected='cancelled'
  - write proposals on none; targets=['cable']; wrong=[]; missing=['cable']; unresolved_reads=[]
  - step 1 skipped: no awaiting_confirmation action for {'object': 'cable', 'operation': 'cancel_order'}
- Final business state: `{"cable": {"status": "unpaid", "auto_renew": false, "entitlement": "充电线", "invoice_status": "not_requested", "refunds": []}}`

### PE-I05 · idempotency_state · 已退款完成后再次退款

- User: “帮我把无线耳机退掉” → {"confirm": {"object": "headphones", "operation": "refund"}} → “无线耳机再帮我退一次款”
- Expected: 第二次申请显示无剩余可退金额
- Actual: actions headphones/refund=completed; routes ['commerce', 'chat']
- Failed metrics: goals, action_tool
- Failure type: **WRONG_ACTION**
- Root cause: INTENT_NOT_EXTRACTED: 第 [5] 步落入通用对话兜底(route=chat)，后续请求未生成操作
- Evidence:
  - goal action:headphones ✗ latest headphones/refund status=completed expected∈['ineligible']; reason=模拟账务已核验；不是支付渠道到账回执。按优惠分摊后的实付金额退款；单品退款不退运费，全单退剩余实付款；已开发票进入调整流程；先等待退货验收，再进入模拟退款处理
- Final business state: `{"headphones": {"status": "delivered", "auto_renew": false, "entitlement": "无线耳机", "invoice_status": "adjustment_required", "refunds": [[25900, "succeeded_simulated"]]}}`

### PE-P01 · policy_technical · 签收后退货期限

- User: “签收后多久内可以退货？”
- Expected: 引用商品退款政策
- Actual: actions none; routes ['commerce']
- Failed metrics: goals, clarification
- Failure type: **UNNECESSARY_CLARIFICATION**（另有 RESPONSE_QUALITY）
- Root cause: POLICY_QUESTION_TREATED_AS_ACTION: 政策咨询被当成售后操作，系统要求选择订单
- Evidence:
  - goal policy: ✗ sources=[] required=['goods-refund']
  - clarification_step=0 (none expected)
- Final business state: `{}`

### PE-P02 · policy_technical · 耳机是否支持降噪

- User: “无线耳机支持降噪吗？”
- Expected: 引用耳机规格文档（无主动降噪）
- Actual: actions none; routes ['chat']
- Failed metrics: goals
- Failure type: **RESPONSE_QUALITY**
- Root cause: NO_POLICY_SOURCE: 回答没有附带政策来源 (routes=['chat'])
- Evidence:
  - goal policy: ✗ sources=[] required=['goods-spec']
- Final business state: `{}`

### PE-P03 · policy_technical · 登录 401

- User: “登录一直报 401 怎么办”
- Expected: 调用只读 lookup_error_code 解释 401
- Actual: actions none; routes ['knowledge']
- Failed metrics: goals, action_tool
- Failure type: **RESPONSE_QUALITY**
- Root cause: TECHNICAL_AGENT_UNAVAILABLE_OFFLINE: 离线模式下没有调用只读错误码工具，响应带 technical_agent_offline 降级标记
- Evidence:
  - goal technical: ✗ tools_used=[] required=lookup_error_code; answer_contains_expected=False; degradations=['local_policy_fallback', 'technical_agent_offline']
- Final business state: `{}`

### PE-P04 · policy_technical · 接口 429

- User: “接口返回 429 是什么意思”
- Expected: 调用只读 lookup_error_code 解释 429
- Actual: actions none; routes ['chat']
- Failed metrics: goals, action_tool
- Failure type: **RESPONSE_QUALITY**
- Root cause: TECHNICAL_QUESTION_NOT_ROUTED: 技术问题落入通用兜底回复，未调用只读工具
- Evidence:
  - goal technical: ✗ tools_used=[] required=lookup_error_code; answer_contains_expected=False; degradations=[]
- Final business state: `{}`

### PE-P05 · policy_technical · 退款后发票处理

- User: “退款以后发票会怎么处理？”
- Expected: 引用发票政策
- Actual: actions none; routes ['commerce']
- Failed metrics: goals, clarification
- Failure type: **UNNECESSARY_CLARIFICATION**（另有 RESPONSE_QUALITY）
- Root cause: POLICY_QUESTION_TREATED_AS_ACTION: 政策咨询被当成售后操作，系统要求选择订单
- Evidence:
  - goal policy: ✗ sources=[] required=['goods-invoice']
  - clarification_step=0 (none expected)
- Final business state: `{}`

## 复现

```bash
cd ResolveFlow
PYTHONPATH=. python scripts/run_product_eval.py            # offline rules，确定性，无需 API Key
PYTHONPATH=. python scripts/run_product_eval.py --live     # 需 AGENT_USE_LLM=1 与 LLM_* 环境变量
```

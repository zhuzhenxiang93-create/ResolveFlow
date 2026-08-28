"""生成 ResolveFlow 四类 Agent 与跨 Agent 的可复现演示金标数据。"""
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()
SOURCE = "resolveflow_synthetic_gold_v2"
LICENSE = "Internal synthetic dataset; no external dialogue text included"
FORBIDDEN_FINANCIAL = ["保证退款", "一定退款", "立刻到账", "立即到账"]


def record(
    record_id: str,
    intent: str,
    group: str,
    primary_agent: str,
    *,
    text: str = "",
    turns: List[str] = None,
    supporting_agents: List[str] = None,
    entities: Dict[str, List[str]] = None,
    knowledge: bool = True,
    escalate: bool = False,
    must_include: List[str] = None,
    must_not_include: List[str] = None,
    scenario_type: str = "single_agent",
    **extra: Any,
) -> Dict[str, Any]:
    item = {
        "id": record_id,
        "source": SOURCE,
        "source_license": LICENSE,
        "language": "zh",
        "domain": "customer_support",
        "expected_intent": intent,
        "expected_group": group,
        "expected_primary_agent": primary_agent,
        "expected_supporting_agents": supporting_agents or [],
        "entities": entities or {},
        "should_use_knowledge": knowledge,
        "should_escalate": escalate,
        "must_include": must_include or [],
        "must_not_include": must_not_include or [],
        "review_status": "pending_human_review",
        "reviewer": "unassigned",
        "created_at": TODAY,
        "synthetic": True,
        "scenario_type": scenario_type,
    }
    if turns is not None:
        item["turns"] = turns
    else:
        item["text"] = text
    item.update(extra)
    return item


def styled_intents(
    prefix: str,
    specifications: Sequence[Tuple[str, str, str, bool, bool, Sequence[str]]],
) -> List[Dict[str, Any]]:
    styles = [
        ("", "，麻烦说明一下。"),
        ("请问", "？"),
        ("我有点着急，", "，请尽快帮我确认。"),
        ("想问下", "，谢谢。"),
    ]
    items: List[Dict[str, Any]] = []
    for intent, group, agent, knowledge, escalate, prompts in specifications:
        for prompt_index, prompt in enumerate(prompts, start=1):
            for style_index, (lead, tail) in enumerate(styles, start=1):
                items.append(record(
                    f"{prefix}-intent-{intent}-{prompt_index:02d}-{style_index}",
                    intent,
                    group,
                    agent,
                    text=f"{lead}{prompt}{tail}",
                    knowledge=knowledge,
                    escalate=escalate,
                ))
    return items


def adversarial(
    prefix: str,
    count: int,
    specifications: Sequence[Tuple[str, str, str, bool, str, List[str]]],
) -> List[Dict[str, Any]]:
    qualifiers = ["", "请严肃处理，", "我很着急，", "不要让我继续等待，", "这已经影响到我了，"]
    items: List[Dict[str, Any]] = []
    for index in range(count):
        intent, group, agent, knowledge, prompt, required = specifications[index % len(specifications)]
        items.append(record(
            f"{prefix}-adversarial-{index + 1:03d}",
            intent,
            group,
            agent,
            text=f"{qualifiers[index % len(qualifiers)]}{prompt}",
            knowledge=knowledge,
            escalate=True,
            must_include=required,
            must_not_include=FORBIDDEN_FINANCIAL if agent == "billing" else ["提供密码", "发送验证码给我"],
        ))
    return items


def dialogs(
    prefix: str,
    count: int,
    templates: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for index in range(count):
        template = templates[index % len(templates)]
        item = record(
            f"{prefix}-dialog-{index + 1:03d}",
            template["intent"],
            template["group"],
            template["agent"],
            turns=list(template["turns"]),
            supporting_agents=list(template.get("supporting_agents", [])),
            entities=dict(template.get("entities", {})),
            knowledge=template.get("knowledge", True),
            escalate=template.get("escalate", False),
            must_include=list(template.get("must_include", [])),
            must_not_include=list(template.get("must_not_include", [])),
            scenario_type=template.get("scenario_type", "single_agent"),
        )
        item["turn_expectations"] = [
            {},
            {"memory_keywords": list(template.get("memory_keywords", []))},
        ]
        items.append(item)
    return items


def general_intents() -> List[Dict[str, Any]]:
    return styled_intents("general", [
        ("order_status", "query", "general", True, False, [
            "订单显示处理中，想知道当前处于哪个环节", "订单一直没有进入下一步，如何查看处理进度",
            "我想确认购买请求是否已经成功受理", "页面显示待处理，应该在哪里看状态说明",
            "订单状态和我预期不一致，想了解含义",
        ]),
        ("logistics", "query", "general", True, False, [
            "物流信息长时间没有更新，应该怎么办", "配送预计何时到达", "快递状态显示异常，需要怎么查询",
            "配送地址附近一直没有进展", "发货后如何查看物流轨迹",
        ]),
        ("account", "account", "general", True, False, [
            "我想修改账户中的显示资料", "怎样更新个人偏好设置", "账户资料页面无法找到编辑入口",
            "我需要调整通知设置", "怎样查看当前绑定的资料",
        ]),
        ("query", "query", "general", True, False, [
            "会员等级是怎样划分的", "积分规则在哪里查看", "客服服务时间是什么时候",
            "平台支持哪些常见功能", "怎样找到帮助中心的说明",
        ]),
        ("complaint", "complaint", "general", False, False, [
            "这次服务体验很差，但我想先说明问题", "客服回复太慢，希望改善", "页面说明让我很困惑",
            "配送体验不符合预期", "我对处理方式感到不满意",
        ]),
        ("feedback", "feedback", "general", False, False, [
            "这次问题解决得很快，想表达感谢", "新版本使用体验不错", "配送提醒很清楚",
            "客服解释得很耐心", "整体流程比以前方便很多",
        ]),
    ])


def technical_intents() -> List[Dict[str, Any]]:
    items = styled_intents("technical", [
        ("technical_login", "technical", "technical", True, False, [
            "登录时出现错误码 401", "验证码一直没有收到", "账户密码确认无误却无法登录",
            "登录后页面立刻退出", "安全验证页面无法继续", "登录按钮点击后没有反应",
            "切换设备后无法完成登录", "登录提示访问被拒绝", "登录界面不断刷新", "身份验证总是失败",
        ]),
        ("technical_crash", "technical", "technical", True, False, [
            "应用打开后立即崩溃", "页面出现错误码 500", "提交操作时界面闪退",
            "更新后程序无法启动", "加载内容时反复白屏", "功能页面一打开就报错",
            "应用卡住无法继续操作", "浏览器页面显示服务器错误", "搜索功能导致页面崩溃", "消息页面无法加载",
        ]),
        ("technical", "technical", "technical", True, False, [
            "页面加载速度很慢", "同步内容在不同设备上不一致", "通知一直没有出现",
            "上传功能没有完成", "搜索结果为空但我确定有内容", "设置保存后没有生效",
            "网络正常但功能持续转圈", "界面显示异常排版", "支付页面无法跳转", "某个功能入口突然消失",
        ]),
    ])
    for item in items:
        if "401" in item["text"] or "500" in item["text"]:
            item["entities"] = {"error_code": ["401" if "401" in item["text"] else "500"]}
    return items


def billing_intents() -> List[Dict[str, Any]]:
    return styled_intents("billing", [
        ("refund", "billing", "billing", True, True, [
            "服务尚未使用，是否可以申请退款", "退款申请提交后需要经历哪些步骤", "退款审核通常需要多长时间",
            "审核完成后会退回到哪里", "服务已经开始使用了一部分，还能否申请退款", "退款申请一直没有更新，应该怎么处理",
            "我想撤回刚提交的退款申请", "购买后发现不适合自己，想了解退款条件", "退款被拒绝后可以如何补充材料",
            "退款处理期间我需要做什么", "退款资格存在争议，怎样申请复核", "退款结果出来前是否需要保留相关材料", "退款流程需要人工确认吗",
        ]),
        ("invoice", "billing", "billing", True, False, [
            "我需要申请电子发票，入口在哪里", "开票前需要准备哪些信息", "发票抬头填写错误后怎样更正",
            "已经申请的发票在哪里查看", "发票申请提交后没有任何状态变化", "我想知道发票开具的处理周期",
            "电子发票是否可以重新下载", "发票内容与购买内容不一致应该找谁处理", "开票申请失败时需要先检查什么",
            "企业开票和个人开票的流程是否不同", "发票更正是否需要重新提交申请", "如何确认发票申请已经受理", "发票文件无法打开怎么办",
        ]),
        ("payment_issue", "billing", "billing", True, True, [
            "支付后页面没有显示成功，担心状态异常", "我发现有一笔无法识别的扣款记录", "同一项服务似乎被重复扣费了",
            "付款时提示失败，但我担心已经被扣款", "支付方式被拒绝，想知道下一步怎么排查", "账单显示的收费项目我看不明白",
            "自动续费的状态和预期不一致", "取消订阅后仍然出现扣费提示", "支付页面反复报错，无法完成购买",
            "我的付款记录和实际使用情况不一致", "支付完成后订单仍显示未处理", "账单出现异常状态，需要核实", "付款渠道提示处理中太久",
        ]),
        ("account_security", "account", "billing", True, True, [
            "我发现账户有陌生设备登录记录", "怀疑账户密码已经泄露，需要立即保护账户", "我没有操作过，却收到安全提醒",
            "账户里出现我不认识的活动记录", "登录后发现设置被改动了，不是我本人操作", "我担心账户正被他人使用",
            "安全验证频繁触发，怀疑有人在尝试登录", "我想尽快冻结账户避免继续风险", "绑定信息似乎被陌生人修改了",
            "我需要确认是否存在异常访问", "账户出现陌生会话记录", "发现异常操作后怎样保护资料", "安全提示连续出现，需要尽快升级",
        ]),
    ])


def escalation_intents() -> List[Dict[str, Any]]:
    return styled_intents("escalation", [
        ("human_handoff", "escalation", "escalation", True, True, [
            "请转人工客服处理", "我需要由人工继续跟进", "不要机器人回复了，帮我联系人工",
            "这个问题需要专员处理", "请把我转给能够处理的人", "我希望有人直接联系我处理",
            "重复说明太多次了，请人工接手", "请创建人工处理请求", "我要求升级给真人客服", "请安排人工复核",
        ]),
        ("escalation", "escalation", "escalation", True, True, [
            "我已经反馈多次仍未解决，需要升级", "这次处理严重影响到我，请负责人跟进", "普通客服无法解决，请升级处理",
            "我需要正式投诉并保留处理记录", "问题反复发生，希望由专员负责", "请将我的诉求反馈给管理人员",
            "我不接受当前方案，需要升级复核", "这是高风险问题，必须有人处理", "问题长期没有结果，需要进一步升级", "请不要再让我重复描述，直接升级",
        ]),
    ])


def all_dialogs() -> Dict[str, List[Dict[str, Any]]]:
    general = dialogs("general", 25, [
        {"intent": "order_status", "group": "query", "agent": "general", "turns": ["订单一直在处理中", "刚才提到的订单状态具体代表什么"], "memory_keywords": ["订单"]},
        {"intent": "logistics", "group": "query", "agent": "general", "turns": ["物流长时间没更新", "这个配送问题下一步应该怎么查询"], "memory_keywords": ["物流"]},
        {"intent": "account", "group": "account", "agent": "general", "turns": ["我想修改账户资料", "前面说的资料修改入口我还是没有找到"], "memory_keywords": ["资料"]},
        {"intent": "query", "group": "query", "agent": "general", "turns": ["会员积分有什么规则", "刚才说的积分什么时候会失效"], "memory_keywords": ["积分"]},
    ])
    technical = dialogs("technical", 25, [
        {"intent": "technical_login", "group": "technical", "agent": "technical", "turns": ["登录出现错误码 401", "刚才的登录错误还在，应该先做哪一步"], "entities": {"error_code": ["401"]}, "memory_keywords": ["登录"]},
        {"intent": "technical_crash", "group": "technical", "agent": "technical", "turns": ["页面出现错误码 500", "刚才的页面错误是否需要升级处理"], "entities": {"error_code": ["500"]}, "memory_keywords": ["页面"]},
        {"intent": "technical", "group": "technical", "agent": "technical", "turns": ["应用加载一直转圈", "前面说的加载问题我已经重试过，下一步怎么办"], "memory_keywords": ["加载"], "escalate": True, "must_include": ["人工"]},
    ])
    billing = dialogs("billing", 40, [
        {"intent": "refund", "group": "billing", "agent": "billing", "turns": ["我想申请退款", "服务还没有使用，我想确认是否符合退款条件"], "memory_keywords": ["退款"], "escalate": False, "must_include": ["审核"], "must_not_include": FORBIDDEN_FINANCIAL},
        {"intent": "invoice", "group": "billing", "agent": "billing", "turns": ["我需要电子发票", "前面说的发票申请提交后，在哪里查看处理进度"], "memory_keywords": ["发票"]},
        {"intent": "payment_issue", "group": "billing", "agent": "billing", "turns": ["我发现扣款记录不对", "刚才提到的扣款不是我认识的项目，需要怎么核实"], "memory_keywords": ["扣款"], "escalate": True, "must_include": ["人工"], "must_not_include": FORBIDDEN_FINANCIAL},
        {"intent": "account_security", "group": "account", "agent": "billing", "turns": ["我怀疑账户被他人访问", "刚才说的异常访问还在担心，我想先保护账户"], "memory_keywords": ["账户"], "escalate": True, "must_include": ["人工"], "must_not_include": FORBIDDEN_FINANCIAL},
    ])
    escalation = dialogs("escalation", 20, [
        {"intent": "human_handoff", "group": "escalation", "agent": "escalation", "turns": ["请转人工处理", "刚才的情况我不想再重复说明，请直接升级"], "memory_keywords": ["人工"], "escalate": True, "must_include": ["人工"]},
        {"intent": "escalation", "group": "escalation", "agent": "escalation", "turns": ["这个问题已经多次没有解决", "前面的问题仍然存在，我需要正式升级处理"], "memory_keywords": ["问题"], "escalate": True, "must_include": ["人工"]},
    ])
    cross_templates = [
        {"intent": "account_security", "group": "account", "agent": "billing", "supporting_agents": ["technical"], "scenario_type": "cross_agent", "turns": ["登录出现异常后，我又发现有不认识的扣款提示", "刚才提到的登录和扣款问题，应该先怎样保护账户"], "memory_keywords": ["账户"], "escalate": True, "must_include": ["人工"], "must_not_include": FORBIDDEN_FINANCIAL},
        {"intent": "human_handoff", "group": "escalation", "agent": "escalation", "supporting_agents": ["billing"], "scenario_type": "cross_agent", "turns": ["退款争议一直没有结果，我要投诉升级", "前面提到的退款争议请不要重复解释，直接转人工"], "memory_keywords": ["退款"], "escalate": True, "must_include": ["人工"], "must_not_include": FORBIDDEN_FINANCIAL},
        {"intent": "technical", "group": "technical", "agent": "technical", "supporting_agents": ["billing"], "scenario_type": "cross_agent", "turns": ["支付页面报错后，账单状态也不一致", "刚才的支付报错和账单状态问题需要怎么一起处理"], "memory_keywords": ["支付"], "escalate": True, "must_include": ["人工"]},
        {"intent": "refund", "group": "billing", "agent": "billing", "supporting_agents": ["general"], "scenario_type": "cross_agent", "turns": ["物流异常一直没有进展，我想申请退款", "前面提到的物流和退款问题，请说明后续处理方式"], "memory_keywords": ["退款"], "escalate": True, "must_include": ["人工审核"], "must_not_include": FORBIDDEN_FINANCIAL},
        {"intent": "account_security", "group": "account", "agent": "billing", "supporting_agents": ["general"], "scenario_type": "cross_agent", "turns": ["账户安全异常后，我还担心发票资料被改动", "刚才的账户安全和发票资料问题，请一起处理"], "memory_keywords": ["账户"], "escalate": True, "must_include": ["人工"]},
    ]
    cross = dialogs("cross-agent", 60, cross_templates)
    return {"general": general, "technical": technical, "billing": billing, "escalation": escalation, "cross": cross}


def policies() -> Dict[str, List[Dict[str, Any]]]:
    common = {"effective_date": TODAY, "source": "ResolveFlow internal demo policy v1", "owner": "ResolveFlow demo team", "review_status": "pending_human_review", "version": "v1", "last_reviewed_at": TODAY}
    def doc(doc_id: str, title: str, category: str, risk: str, content: str, search_terms: List[str]) -> Dict[str, Any]:
        return {"id": doc_id, "title": title, "category": category, "risk_level": risk, "content": f"这是 ResolveFlow 演示政策，不代表真实业务规则。{content}", "search_terms": search_terms, **common}
    return {
        "general": [
            doc("demo-order-status", "演示政策：订单状态", "order_status", "medium", "订单状态应通过正规查询入口确认，客服不得臆测处理进度。", ["订单", "购买", "处理中", "待处理", "处理进度", "受理", "状态含义"]),
            doc("demo-logistics", "演示政策：物流配送", "logistics", "medium", "物流异常应引导用户查询并在持续异常时升级处理。", ["物流", "配送", "包裹", "快递", "运输", "发货", "送达", "轨迹", "运送"]),
            doc("demo-account-profile", "演示政策：账户资料", "account", "medium", "账户资料修改应使用正规设置入口，不得要求用户提供敏感凭据。", ["账户资料", "个人资料", "账号资料", "偏好", "通知", "编辑", "修改", "设置"]),
            doc("demo-membership", "演示政策：会员与积分", "query", "low", "会员和积分说明以当前帮助中心规则为准。", ["会员", "积分", "等级", "权益", "等级变化", "积分规则"]),
        ],
        "technical": [
            doc("demo-login-sop", "演示 SOP：登录与验证码", "technical_login", "high", "登录失败时先检查凭据、网络和验证码；不得索要密码或验证码。", ["登录", "账号", "验证码", "401", "认证", "身份验证", "无法登录", "登不进去"]),
            doc("demo-crash-sop", "演示 SOP：崩溃与服务器错误", "technical_crash", "high", "崩溃或服务器错误应提供基本排障步骤，持续失败时升级人工。", ["崩溃", "闪退", "500", "白屏", "卡死", "服务器错误", "无法启动", "报错"]),
            doc("demo-network-sop", "演示 SOP：网络与加载", "technical", "medium", "加载异常应检查网络、缓存和版本状态。", ["加载", "转圈", "网络", "缓存", "版本", "同步", "上传", "通知", "页面异常"]),
            doc("demo-payment-page-sop", "演示 SOP：支付页面技术故障", "technical", "high", "支付页面技术问题需要与账单状态核实，不能臆测交易结果。", ["支付页面", "付款页", "结算", "支付报错", "交易结果", "跳转", "账单状态", "页面卡住"]),
        ],
        "billing": [
            doc("demo-refund-eligibility", "演示政策：退款资格", "refund", "high", "未使用的服务可以提交退款申请，是否通过由人工审核决定。客服不得保证退款结果。", ["退款", "退钱", "退回", "未使用", "资格", "申请退款", "用了一部分", "保证退款"]),
            doc("demo-refund-timeline", "演示政策：退款处理时效", "refund", "high", "退款申请需要人工审核；处理进度以实际支付渠道状态为准。客服不得承诺立刻到账。", ["退款", "到账", "时效", "多久", "进度", "审核", "处理状态", "支付渠道"]),
            doc("demo-payment-troubleshooting", "演示政策：支付失败与重复扣款", "payment_issue", "critical", "支付失败、重复扣款或无法识别扣款均需先核实交易状态。涉及异常扣款时应升级人工处理。", ["支付", "扣款", "扣费", "重复扣款", "多扣", "收费", "账单", "交易", "支付失败", "异常收费"]),
            doc("demo-invoice", "演示政策：发票申请与更正", "invoice", "medium", "用户可按正规入口提交电子发票申请和更正请求。客服不得伪造发票内容。", ["发票", "票据", "抬头", "开票", "电子票据", "更正", "下载", "开具"]),
            doc("demo-account-security", "演示政策：账户安全与升级", "account_security", "critical", "出现陌生设备、异常访问或疑似盗用时，应建议用户保护账户并升级人工处理。客服不得索要密码或验证码。", ["安全", "盗用", "陌生设备", "异常访问", "可疑登录", "密码", "验证码", "风险"]),
        ],
        "escalation": [
            doc("demo-escalation-sop", "演示 SOP：人工升级", "escalation", "critical", "明确转人工、重复失败、异常扣款和账户安全风险应升级。转交时保留问题摘要，客服不得索要密码或验证码。", ["人工", "升级", "专员", "投诉", "重复失败", "工单", "问题摘要", "转交", "自动处理"]),
        ],
    }


def rag_queries(documents: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """为每份演示政策建立 10 条不复述标题的自然检索问法。"""
    query_sets = {
        "demo-order-status": [
            "购买后一直显示处理中代表什么", "去哪里看我的购买处理到了哪一步",
            "页面显示待处理时应如何确认进度", "能否帮我理解当前购买状态",
            "为什么记录没有进入下一环节", "怎样核对购买请求是否已受理",
            "状态和预期不同应该先查什么", "处理进度只能通过什么方式确认",
            "客服能直接猜测我的处理进度吗", "我想了解各个处理状态的含义",
        ],
        "demo-logistics": [
            "配送轨迹很久不更新要怎么办", "发出后如何查看包裹走到哪里",
            "预计送达时间在哪里确认", "快递显示异常该先做什么",
            "配送长时间没有变化是否需要升级", "如何查询发货后的运输记录",
            "包裹一直卡在附近站点怎么处理", "物流进展异常时客服可以怎样协助",
            "配送问题不能马上解决时怎么办", "我需要核对运送状态的正规入口",
        ],
        "demo-account-profile": [
            "怎样修改账号里的个人资料", "通知偏好设置要去哪里调整",
            "找不到资料编辑入口怎么办", "账户页面能更新哪些信息",
            "修改显示信息是否需要提供密码", "如何查看当前保存的资料",
            "个人设置保存后应该怎样确认", "客服能否代替我改资料",
            "资料变更应该使用哪个正规入口", "更新账号偏好时有哪些安全注意事项",
        ],
        "demo-membership": [
            "会员等级是如何计算的", "积分获得和使用规则是什么",
            "现在的会员权益去哪里看", "积分说明是否会随帮助中心更新",
            "我想弄清楚等级变化原因", "常见权益的官方说明在哪里",
            "会员相关问题应参考什么规则", "积分记录和规则不一致如何核实",
            "不同等级有哪些差别", "怎么确认当前有效的积分说明",
        ],
        "demo-login-sop": [
            "输入正确账号仍然进不去怎么办", "收不到验证短信应先排查什么",
            "出现 401 后该怎样处理", "登录页面反复验证失败",
            "换设备后无法完成身份验证", "账号登录失败时需要检查网络吗",
            "客服会要求我提供验证码吗", "登录按钮没有反应怎么排查",
            "认证失败后能否先做基础检查", "忘记凭据之外的登录障碍如何处理",
        ],
        "demo-crash-sop": [
            "应用一打开就退出怎么办", "页面报 500 代表什么",
            "提交操作时总是闪退如何处理", "服务端异常持续出现要不要升级",
            "更新后软件无法启动怎么排查", "白屏和崩溃先检查哪些项目",
            "浏览器打开功能页就报错", "反复发生系统错误时客服如何处理",
            "基础排查无效后应该找谁", "程序卡死无法继续操作怎么办",
        ],
        "demo-network-sop": [
            "页面一直转圈却没有内容", "网络正常但功能加载很慢",
            "同步结果在设备间不一致", "清理缓存能解决加载问题吗",
            "设置保存后没有生效怎么查", "内容显示异常需要确认版本吗",
            "上传一直没有完成怎么办", "通知没有出现是否与网络有关",
            "普通加载问题有哪些基础排查步骤", "某个入口突然不显示如何处理",
        ],
        "demo-payment-page-sop": [
            "付款页报错时能否判断交易成功", "结算页面打不开应先核实什么",
            "支付过程卡住但账单状态不明", "技术故障和收费记录不一致怎么办",
            "点击付款没有跳转如何排查", "页面错误后不要臆测什么结果",
            "购买页面加载异常需要技术支持吗", "支付界面持续失败如何处理",
            "账单状态需要和哪一方核对", "交易结果不确定时客服应该怎样说明",
        ],
        "demo-refund-eligibility": [
            "服务还没有使用可以申请退回吗", "购买后发现不合适怎样提交申请",
            "退款是否一定会通过", "哪些情况需要人工核实退回资格",
            "我想知道未使用项目能不能退", "已经用了一部分还能申请吗",
            "资格有争议时怎样继续处理", "提交退回请求前需要了解什么",
            "客服可以保证退钱吗", "退款结果由谁最终决定",
        ],
        "demo-refund-timeline": [
            "申请提交后多久会有进展", "退回款项什么时候到原支付方式",
            "审核期间如何查看处理状态", "为什么不能承诺马上到账",
            "退款卡在处理中应如何跟进", "资金退回需要支付渠道确认吗",
            "申请后需要经过人工审核吗", "退款进度长时间未变怎么办",
            "已经审核后还要等多久", "怎样理解处理时效的说明",
        ],
        "demo-payment-troubleshooting": [
            "同一服务好像被扣了两次怎么办", "有笔交易不是我操作的需要怎么处理",
            "付款失败但担心已经扣钱", "收费项目看不明白该如何核实",
            "异常扣费为什么要人工处理", "支付方式被拒绝后先查什么",
            "取消后仍有收费提示怎么办", "账单和实际使用不一致如何处理",
            "重复收费能直接承诺退回吗", "交易状态不确定时客服应如何说明",
        ],
        "demo-invoice": [
            "电子票据要从哪里申请", "抬头填错后怎样更正",
            "开具申请提交后去哪里看状态", "票据文件打不开怎么办",
            "企业和个人申请流程一样吗", "申请失败时先检查哪些信息",
            "能否重新下载已开具的文件", "内容和购买记录不一致如何处理",
            "客服可以替我伪造票据吗", "开具通常需要经过哪些正规步骤",
        ],
        "demo-account-security": [
            "发现陌生设备登录账号该怎么办", "怀疑账号被人使用需要立即做什么",
            "异常访问后是否必须人工处理", "账号安全风险可以索要验证码吗",
            "我担心资料被未授权修改", "发现可疑登录记录如何保护账户",
            "客服会让我提供密码核验吗", "疑似盗用和收费异常同时出现怎么办",
            "安全问题升级后要保留哪些情况说明", "怎样处理从未见过的访问活动",
        ],
        "demo-escalation-sop": [
            "我明确要求人工处理应该怎样转交", "问题反复失败后能否升级专员",
            "异常收费需要谁继续跟进", "安全风险为什么不能只靠自动回复",
            "转交时客服应保留哪些上下文", "人工介入前不该向我索要什么",
            "投诉升级后还要重复描述吗", "哪些高风险问题必须交由人工",
            "客服如何写清楚问题摘要", "什么时候需要结束自动处理并升级",
        ],
    }
    items = []
    for domain_docs in documents.values():
        for doc in domain_docs:
            queries = query_sets.get(doc["id"], [])
            if len(queries) != 10:
                raise ValueError(f"RAG 查询必须为每份文档提供 10 条: {doc['id']}")
            for index, query in enumerate(queries, start=1):
                items.append({"id": f"rag-{doc['id']}-{index:02d}", "query": query, "expected_document_id": doc["id"], "top_k": 3})
    return items


def english_seed_cases() -> List[Dict[str, Any]]:
    prompts = [
        ("refund", "billing", "billing", "I need to understand refund eligibility for an unused service."),
        ("invoice", "billing", "billing", "Where can I request an electronic invoice?"),
        ("payment_issue", "billing", "billing", "I do not recognize a charge and need it reviewed."),
        ("account_security", "account", "billing", "I noticed an unfamiliar device signing in to my account."),
        ("technical_login", "technical", "technical", "I cannot sign in and receive an authentication error."),
        ("technical_crash", "technical", "technical", "The application crashes when I open a page."),
        ("order_status", "query", "general", "I need to understand my order processing status."),
        ("human_handoff", "escalation", "escalation", "Please transfer this request to a human agent."),
    ]
    rows = []
    for index, (intent, group, agent, text) in enumerate(prompts, start=1):
        row = record(
            f"multi-agent-seed-en-{index:02d}", intent, group, agent, text=text,
            knowledge=True, escalate=agent == "escalation" or intent == "account_security",
        )
        row["language"] = "en"
        row["source"] = "resolveflow_synthetic_taxonomy_reference"
        row["source_license"] = "Taxonomy reference: Bitext CDLA-Sharing-1.0; BANKING77 CC-BY-4.0"
        rows.append(row)
    return rows


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records) + "\n", encoding="utf-8")


def main() -> None:
    general_adv = adversarial("general", 30, [
        ("complaint", "complaint", "general", False, "物流和订单说明互相矛盾，请给我清楚解释", []),
        ("account", "account", "general", True, "我找不到账户资料入口，请不要让我提供敏感信息", []),
        ("escalation", "escalation", "escalation", False, "普通咨询已经反复失败，请升级处理", ["人工"]),
    ])
    technical_adv = adversarial("technical", 40, [
        ("technical_login", "technical", "technical", True, "登录错误一直出现，重试后仍然失败，需要进一步处理", ["人工"]),
        ("technical_crash", "technical", "technical", True, "错误码 500 反复出现，基础排查无效", ["人工"]),
        ("technical", "technical", "technical", True, "支付页面加载异常，不要臆测账单结果", ["人工"]),
    ])
    billing_adv = adversarial("billing", 60, [
        ("payment_issue", "billing", "billing", True, "有笔不认识的扣款，必须人工核实真实状态", ["人工"]),
        ("account_security", "account", "billing", True, "账号可能被盗用，请不要向我索要密码或验证码", ["人工"]),
        ("human_handoff", "escalation", "escalation", True, "退款问题拖了很久，我要求现在转人工处理", ["人工"]),
        ("refund", "billing", "billing", True, "退款资格有争议，不接受没有审核的保证结果", ["人工审核"]),
        ("invoice", "billing", "billing", True, "开票失败多次，请给出正规更正流程，不要伪造任何内容", []),
    ])
    escalation_adv = adversarial("escalation", 40, [
        ("human_handoff", "escalation", "escalation", True, "请立即转人工，不要继续要求我重复描述", ["人工"]),
        ("escalation", "escalation", "escalation", True, "账户风险和异常扣款同时出现，需要升级专员处理", ["人工"]),
    ])
    documents = policies()
    all_dialog_sets = all_dialogs()
    outputs = {
        "general_intents_zh.jsonl": general_intents(),
        "general_adversarial_zh.jsonl": general_adv,
        "technical_intents_zh.jsonl": technical_intents(),
        "technical_adversarial_zh.jsonl": technical_adv,
        "billing_intents_zh.jsonl": billing_intents(),
        "billing_adversarial_zh.jsonl": billing_adv,
        "escalation_intents_zh.jsonl": escalation_intents(),
        "escalation_adversarial_zh.jsonl": escalation_adv,
        "general_dialogs_zh.jsonl": all_dialog_sets["general"],
        "technical_dialogs_zh.jsonl": all_dialog_sets["technical"],
        "billing_dialogs_zh.jsonl": all_dialog_sets["billing"],
        "escalation_dialogs_zh.jsonl": all_dialog_sets["escalation"],
        "cross_agent_dialogs_zh.jsonl": all_dialog_sets["cross"],
    }
    for filename, rows in outputs.items():
        write_jsonl(ROOT / "data/golden" / filename, rows)
    seed_cases = english_seed_cases()
    write_jsonl(ROOT / "data/processed/multi_agent_seed_en.jsonl", seed_cases)
    billing_seed_cases = []
    for index, row in enumerate((row for row in seed_cases if row["expected_primary_agent"] == "billing"), start=1):
        billing_row = dict(row)
        billing_row["id"] = f"billing-seed-en-{index:02d}"
        billing_seed_cases.append(billing_row)
    write_jsonl(
        ROOT / "data/processed/billing_seed_en.jsonl",
        billing_seed_cases,
    )
    for domain, rows in documents.items():
        path = ROOT / "data/knowledge" / f"{domain}_policy_v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_jsonl(ROOT / "data/eval/rag_queries.jsonl", rag_queries(documents))
    from split_gold_data import main as split_gold_data
    split_gold_data()
    print("generated multi-agent gold: general=120/25/30, technical=120/25/40, billing=208/40/60, escalation=80/20/40, cross=60")


if __name__ == "__main__":
    main()

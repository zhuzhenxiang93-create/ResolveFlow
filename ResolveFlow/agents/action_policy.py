"""Typed business contracts and verifiers; model output never grants write permission."""
import re
from typing import List

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: StrictStr = Field(min_length=1, max_length=100)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    goal: str
    agent: str
    tool: str
    dependencies: List[str] = Field(default_factory=list)
    required_information: List[str] = Field(default_factory=lambda: ["order_id"])
    success_condition: str


class PlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: List[Step] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=240)


TOOLS = {
    "query_subscription": ("technical", False, "Read plan and current entitlements; does not change them."),
    "check_service": ("technical", False, "Read service availability before diagnosing entitlement problems."),
    "query_billing": ("billing", False, "Read captured charge and refund counts; does not request a refund."),
    "query_renewal": ("billing", False, "Read auto-renewal status. Does not cancel anything."),
    "cancel_renewal": ("billing", True, "Disable future renewal after fresh query and specific user confirmation. Retain current entitlements; no refund."),
    "sync_entitlements": ("technical", True, "Synchronize entitlements only with explicit user repair permission, fresh subscription and healthy service evidence."),
    "request_refund": ("billing", True, "Request human approval for ONE verified duplicate charge; does not refund immediately. Requires explicit refund request."),
}


def understand(message):
    text = message.lower()
    goals = {}
    readonly = bool(re.search(r"只.*查|不.*(操作|修改|退款|同步|修复)|仅.*查|only.*(check|query)|do not|don't|without", text))
    readonly = readonly or bool(re.search(r"如何|怎么|能否|可否|是否|能.*吗|可以.*吗|how\b|can\b|could\b|should\b", text))
    policy = bool(re.search(r"政策|规则|policy|退款条件|有什么权益|套餐区别|怎么退订|如何退订|怎么取消|如何取消|退款.*多久|退款.*条件", text))
    if policy:
        goals["policy"] = "read"
        if not re.search(r"查.*(账单|订阅|续费|状态)|请.*(修复|同步|关闭|退款)", text):
            return goals
    if re.search(r"续费|退订|取消订阅|renew", text):
        goals["renewal"] = "cancel" if not readonly and re.search(r"关闭|停止|取消|退订|cancel|disable", text) else "read"
    if re.search(r"权益|功能|升级|订阅|entitlement|subscription|upgrade", text):
        goals["entitlement"] = "repair" if not readonly and re.search(r"修复|同步|恢复权益|fix|sync|repair", text) else "read"
    if re.search(r"扣|退款|账单|refund|charg|bill", text):
        goals["billing"] = "refund" if not readonly and re.search(r"请.*退款|申请退款|退还|refund (me|the|my)|request.*refund", text) else "read"
    if re.search(r"服务|宕机|service|outage", text):
        goals["service"] = "read"
    return goals


def plan_for(task):
    steps = []
    goals = task["goals"]
    def add(tool, goal, deps=()):
        steps.append(Step(id=tool, goal=goal, agent=TOOLS[tool][0], tool=tool,
                          dependencies=list(deps), success_condition=goal + ":" + goals[goal]).model_dump())
    if "entitlement" in goals:
        add("query_subscription", "entitlement")
        add("check_service", "service" if "service" in goals else "entitlement")
        if goals["entitlement"] == "repair":
            add("sync_entitlements", "entitlement", ("query_subscription", "check_service"))
    if "service" in goals and not any(s["tool"] == "check_service" for s in steps):
        add("check_service", "service")
    if "billing" in goals:
        add("query_billing", "billing")
        if goals["billing"] == "refund":
            add("request_refund", "billing", ("query_billing",))
    if "renewal" in goals:
        add("query_renewal", "renewal")
        if goals["renewal"] == "cancel":
            add("cancel_renewal", "renewal", ("query_renewal",))
    return steps


def validate_plan(steps, task):
    parsed = [Step.model_validate(s).model_dump() for s in steps]
    if not 1 <= len(parsed) <= 8:
        raise ValueError("Plan must contain 1..8 steps")
    allowed = {s["tool"]: s for s in plan_for(task)}
    ids = {s["id"] for s in parsed}
    if len(ids) != len(parsed) or len({s["tool"] for s in parsed}) != len(parsed):
        raise ValueError("Duplicate plan step")
    by_id = {s["id"]: s for s in parsed}
    for s in parsed:
        template = allowed.get(s["tool"])
        if not template or s["agent"] != template["agent"] or s["goal"] != template["goal"]:
            raise ValueError("Unauthorized plan step")
        if s["required_information"] != template["required_information"] or s["success_condition"] != template["success_condition"]:
            raise ValueError("Business information and success contracts are immutable")
        if any(d not in ids for d in s["dependencies"]):
            raise ValueError("Unknown dependency")
        dependency_tools = {by_id[d]["tool"] for d in s["dependencies"]}
        if not set(template["dependencies"]) <= dependency_tools:
            raise ValueError("Missing business prerequisite")
    visited, active = set(), set()
    def visit(key):
        if key in active:
            raise ValueError("Cyclic plan")
        if key in visited:
            return
        active.add(key)
        for dep in by_id[key]["dependencies"]:
            visit(dep)
        active.remove(key)
        visited.add(key)
    for key in ids:
        visit(key)
    if set(allowed) != {s["tool"] for s in parsed}:
        raise ValueError("Plan omits requested work")
    return parsed


def verify(task, order, seen):
    results = {}
    for goal, mode in task["goals"].items():
        if goal == "policy":
            results[goal] = bool(task.get("policy_answer"))
        elif goal == "entitlement":
            results[goal] = ("query_subscription" in seen if mode == "read" else
                             order["entitlement"] == order["plan"] and order["service"] == "healthy")
        elif goal == "billing":
            results[goal] = ("query_billing" in seen if mode == "read" else order["charges"] - order["refunds"] == 1)
        elif goal == "service":
            results[goal] = "check_service" in seen
        elif goal == "renewal":
            results[goal] = ("query_renewal" in seen if mode == "read" else order.get("auto_renew") is False)
    return results

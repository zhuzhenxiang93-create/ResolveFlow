"""Human-readable task summaries; raw audit records remain available separately."""
import time

LABELS = {"entitlement": "权益", "billing": "退款/账单", "renewal": "续费", "policy": "咨询", "service": "服务状态"}


def summary(payload, reviewer=False):
    if "tasks" in payload:
        return "\n".join(t["id"] + " | " + t["status"] + " | " + t.get("response", "") for t in payload["tasks"]) or "没有待处理任务。"
    task = payload.get("task", payload)
    if "status" not in task:
        return str(payload.get("detail", payload))
    lines = [task.get("response", ""), "任务: " + task["id"] + " | 状态: " + task["status"]]
    verification = task.get("verification", {})
    if verification:
        lines.append("进度: " + "，".join(LABELS.get(k, k) + ("已核验" if v else "未完成") for k, v in verification.items()))
    if reviewer:
        order = payload.get("current_order")
        if order:
            lines.append("当前订单: " + order["id"] + " | 版本: " + str(order["version"]))
            lines.append("套餐/权益: %s/%s | 扣款: %s笔 | 退款: %s笔" % (order["plan"], order["entitlement"], order["charges"], order["refunds"]))
        else:
            lines.append("当前订单快照未加载；/show 刷新后再审核。")
        approvals = task.get("approvals") or {}
        if approvals:
            # Independent goals can each have their own approval now; list
            # all of them rather than assuming there is only ever one.
            for approval in approvals.values():
                lines.append("审批: %s | %s | %s" % (approval["id"], approval["status"], "已过期" if approval["expires_at"] <= time.time() else "有效期内，提交时重新校验"))
                lines.append("对象: %s | 动作: %s | 笔数: %s" % (approval["order_id"], approval["action"], approval["count"]))
        else:
            lines.append("当前没有可批准的申请。")
        for consent in (task.get("confirmations") or {}).values():
            lines.append("用户确认: %s | %s" % (consent["tool"], consent["status"]))
        lines.append("历史工具记录: %s条（不等于当前状态；/debug 查看原始证据）" % len(task.get("evidence", [])))
        latest = {e["tool"]: e for e in task.get("evidence", [])}
        for tool, e in list(latest.items())[-5:]:
            lines.append("证据: %s | %s | 订单版本%s | %s秒前 | 来源:%s" % (
                tool, "已失效" if e.get("invalidated") else "历史成功" if e.get("success") else "失败",
                e.get("order_version", "未知"), max(0, int(time.time() - e.get("timestamp", time.time()))), e.get("source", "未知")))
        if task.get("attempts"):
            error = task["attempts"][-1]
            if error.get("error_type"):
                lines.append("最近异常: " + error["error_type"] + " | 纠正次数: " + str(task.get("no_tool_corrections", 0)))
    status = task["status"]
    if status == "awaiting_confirmation":
        pending = [c for c in task.get("confirmations", {}).values() if c["status"] == "pending"]
        if reviewer:
            lines.append("下一步: 等待用户对具体操作重新确认。")
        else:
            # Independent goals can each have their own pending confirmation
            # at once; list every one instead of assuming there is only one.
            lines.extend("同意: /confirm " + c["id"] + "\n拒绝: /reject " + c["id"] for c in pending)
    elif status == "awaiting_approval":
        lines.append("下一步: /approve 审批ID 或 /reject 审批ID" if reviewer else "下一步: 等待独立人工审批，之后 /continue；这里不能自行批准。")
    elif status == "needs_human":
        lines.append("下一步: 先排查停止原因，核实原目标后再决定是否 /release 任务ID。" if reviewer else "下一步: 联系审核员处理；不要重复提交申请。")
    elif status == "completed":
        lines.append("下一步: 统一会话可直接继续提问；独立任务模式使用 /new 完整问题。" if not reviewer else "本任务已完成，不要重复审批。")
    if any(r.get("server_guard") for r in task.get("interpretations", [])):
        lines.append("目标经服务端安全校正，不计为模型独立识别正确。")
    if task.get("fallback_count"):
        lines.append("注意: 本任务曾使用规则降级。")
    return "\n".join(lines)

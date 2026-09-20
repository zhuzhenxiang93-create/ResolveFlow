"""Scoped extractive answers using the existing lexical retriever, no new engine."""
import json
import re
from pathlib import Path

from mcp.hybrid_retriever import BM25Index

TOPICS = ("plans", "renewal", "refund", "troubleshooting", "safety")


def is_consultation_only(message):
    text = message.lower()
    how_to = re.search(r"怎么|如何|怎样|假如|假设|如果|(?:解释|介绍|说明|了解|咨询)[^，。；!?\n]*?(?:政策|规则|条件|流程)|how (?:to|do|can|should)|what if|suppose", text)
    # Informational clauses do not request an operation; retain separate action clauses.
    action_text = re.sub(r"(?:解释|介绍|说明|了解|咨询)[^，。；!?\n]*?(?:政策|规则|条件|流程)", "", text)
    requested_action = re.search(r"(?:(?<!申)请|帮我|替我|麻烦)[^，。；!?\n]*(?:关闭|取消|停止|修复|同步|退款|查)|(?:please|for me).*(?:cancel|disable|repair|refund|check)", action_text)
    return bool(how_to and not requested_action and policy_topics(text))


_SUBSCRIPTION_SIDE = r"会员费|会员|订阅|权益|套餐|续费|自动续费|entitlement|subscription|renewal"
_GOODS_SIDE = r"商品|订单|快递|包裹|退货|物流|货物|order|shipping|delivery"


def refund_target_is_ambiguous(message):
    """True only when "退款" appears with no qualifier on either side — neither a
    subscription-side term (会员/订阅/会员费/权益/套餐/续费, matching the same domain
    vocabulary policy_topics() already uses for "plans"/"renewal") nor a goods-side
    term (商品/订单/快递/包裹/退货/物流). A message like "怎么退款订阅" or "修复权益
    并申请退款" already names its side and is not ambiguous; only a bare "能退款吗" is.
    Real disambiguation for that bare case is a DB lookup (does this user have any
    subscription order at all), not a better guess from text — this function only
    decides whether that lookup is needed."""
    text = message.lower()
    if not re.search(r"退款|退钱|refund", text):
        return False
    return not re.search(_SUBSCRIPTION_SIDE, text) and not re.search(_GOODS_SIDE, text)


def policy_refund_is_actually_goods(message, policy_topics_list):
    """True when GoalInterpreter classified a message as policy:read about "refund"
    but the message text itself has an explicit goods-side qualifier and no
    subscription-side one — a known real-model confusion pattern where the words
    "退款政策" pull toward policy:read (this platform's own subscription vocabulary)
    even when the message explicitly says it's about merchandise, not membership.
    Unlike refund_target_is_ambiguous (no qualifier at all -> ask or check DB), this
    is the opposite case: an explicit qualifier the model already had and ignored ->
    a plain deterministic override, no DB lookup or clarification needed."""
    if "refund" not in policy_topics_list:
        return False
    text = message.lower()
    return bool(re.search(_GOODS_SIDE, text)) and not re.search(_SUBSCRIPTION_SIDE, text)


def benefits_are_constraint(message):
    text = message.lower()
    preserve = re.search(r"(?:保留|保持|不影响|不要改变|别动)[^，。；!?]{0,25}(?:权益|会员|功能)|(?:retain|keep|preserve).*(?:benefit|entitlement)", text)
    inspect = re.search(r"(?:查|核实|检查|确认)[^，。；!?]{0,20}(?:权益|会员|套餐)|(?:check|query|inspect).*(?:benefit|entitlement)", text)
    return bool(preserve and not inspect)


def policy_topics(message):
    patterns = {"plans": r"套餐|权益|会员|升级|功能|plan|feature",
                "renewal": r"续费|退订|取消订阅|到期|renew|cancel.*subscription",
                "refund": r"退款|扣款|退钱|refund|charge",
                "troubleshooting": r"故障|未生效|无法使用|宕机|troubleshoot|outage",
                "safety": r"审批|确认|授权|安全|approval|consent"}
    return [topic for topic, pattern in patterns.items() if re.search(pattern, message.lower())]


class SubscriptionKnowledge:
    def __init__(self, path=None):
        path = path or Path(__file__).resolve().parents[1] / "data/knowledge/subscription_service_v1.json"
        docs = json.loads(Path(path).read_text())
        self.index = BM25Index()
        self.index.upsert([dict(chunk_id=d["id"], document_id=d["id"], title=d["title"],
                                content=d["content"], topic=d["topic"], version=d["version"],
                                metadata={"search_terms": d["search_terms"]}) for d in docs])

    def answer(self, topics):
        sources = []
        for topic in dict.fromkeys(topics):
            if topic not in TOPICS:
                raise ValueError("Unknown policy topic")
            hits = [d for d in self.index.search(topic, top_k=10) if d["topic"] == topic]
            if not hits:
                return None
            d = hits[0]
            sources.append({k: d[k] for k in ("document_id", "title", "content", "version", "topic")})
        if not sources:
            return None
        return {"answer": "\n".join(d["content"] + " [" + d["document_id"] + "]" for d in sources),
                "sources": sources, "mode": "extractive_internal_policy", "simulated": True}

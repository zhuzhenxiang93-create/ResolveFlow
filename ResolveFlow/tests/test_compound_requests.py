"""Compound requests: GoalInterpreter's general_remainder lets a single turn get both
its subscription-business part (handled by ActionRuntime/RAG) and an independent
ordinary-support part (handled by the old multi-agent orchestrator's own compound-
request machinery) genuinely answered in one merged reply, instead of the business
goal silently swallowing the whole message. Also covers the "退款" target-ambiguity
guard, which resolves a message with no qualifier on either side (no 会员/订阅, no
商品/订单/物流) using real subscription state instead of a model guess."""
import tempfile
import unittest
from unittest.mock import AsyncMock

from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from agents.goal_interpreter import GoalProposal
from agents.subscription_knowledge import policy_refund_is_actually_goods, refund_target_is_ambiguous


class Scripted:
    """Deterministic goal interpreter stub, mirroring test_conversation_intent_merge.py."""
    client = True

    def __init__(self, goals=None, unsupported=None, policy_topics=None, remainder=None):
        self.goals = goals or {}
        self.unsupported = unsupported or []
        self.policy_topics = policy_topics or []
        self.remainder = remainder

    async def interpret(self, message, context):
        return (GoalProposal(goals=self.goals, unsupported_requests=self.unsupported,
                              policy_topics=self.policy_topics, general_remainder=self.remainder),
                {"mode": "mock", "calls": 0, "usage": []})


class RefundTargetIsAmbiguousTests(unittest.TestCase):
    def test_bare_mention_is_ambiguous(self):
        self.assertTrue(refund_target_is_ambiguous("能退款吗"))

    def test_subscription_qualifier_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("怎么退款订阅？"))
        self.assertFalse(refund_target_is_ambiguous("会员费能退吗"))

    def test_goods_qualifier_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("这个订单能退款吗"))
        self.assertFalse(refund_target_is_ambiguous("商品退货退款怎么办"))


class PolicyRefundIsActuallyGoodsTests(unittest.TestCase):
    """Live testing found a real, non-deterministic model-confusion pattern: "退款政策"
    pulls GoalInterpreter toward policy:read (subscription-only) even when the message
    explicitly names a goods-side qualifier — sometimes correctly landing on empty
    goals + general_remainder, sometimes not, across identical repeated calls. This
    function is the deterministic correction wired into ConversationService below."""

    def test_goods_qualifier_with_refund_topic_is_flagged(self):
        self.assertTrue(policy_refund_is_actually_goods("商品退款政策是什么", ["refund"]))

    def test_subscription_qualifier_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("会员费退款政策是什么", ["refund"]))

    def test_no_refund_topic_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("商品什么时候到", ["renewal"]))

    def test_no_goods_qualifier_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("退款政策是什么", ["refund"]))

    def test_no_refund_mention_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("怎么查物流"))


class UnifiedCompoundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from business.commerce import CommerceStore
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=ActionRuntime(self.tmp.name+'/db');self.store=CommerceStore(self.runtime.path)
        self.rows=self.store.seed('user');self.service=ConversationService(self.runtime)
        self.pro=next(o for o in self.rows if o['id'].endswith('pro'))
        self.goods=next(o for o in self.rows if o['id'].endswith('headphones'))
    async def test_two_domains_keep_independent_goals(self):
        r=await self.service.send('user','无线耳机退掉，Pro 月度会员别续了')
        self.assertEqual(len(r['commerce_case']['operations']),2)
    async def test_refund_plus_logistics_does_not_refund_goods(self):
        r=await self.service.send('user','Pro 月度会员退款，无线耳机只查物流')
        self.assertEqual(len(r['commerce_case']['operations']),1)
        self.assertEqual(r['commerce_case']['operations'][0]['quote']['object_id'],self.pro['id'])
        self.assertIn('DEMO-headphones',r['response'])
    async def test_support_receives_only_independent_remainder(self):
        from types import SimpleNamespace
        self.service.answer_orchestrator=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(response='技术说明',tool_traces=[])))
        r=await self.service.send('user','Pro 月度会员退款，另外登录报错401')
        self.assertEqual(self.service.answer_orchestrator.run.await_args.args[0].message,'登录报错401')
        self.assertIn('待用户确认',r['response']);self.assertIn('技术说明',r['response'])
    async def test_support_failure_does_not_lose_case(self):
        from types import SimpleNamespace
        self.service.answer_orchestrator=SimpleNamespace(run=AsyncMock(side_effect=RuntimeError()))
        r=await self.service.send('user','Pro 月度会员退款，另外登录报错401')
        self.assertIsNotNone(r['commerce_case']);self.assertIn('support_unavailable',r['degradations'])
    async def test_no_remainder_no_extra_agent(self):
        self.service.answer_orchestrator=AsyncMock()
        await self.service.send('user','Pro 月度会员退款');self.service.answer_orchestrator.run.assert_not_called()
    async def test_unknown_goods_never_claim_subscription_only(self):
        r=await self.service.send('user','请帮我办理未知礼盒的退货')
        self.assertNotIn('仅支持订阅',r['response']);self.assertIsNone(r['commerce_case'])
    async def test_no_catalog_does_not_redirect_to_general(self):
        r=await self.service.send('empty','我要退款');self.assertEqual(r['route'],'commerce');self.assertEqual(r['status'],'awaiting_clarification')
    async def test_bare_refund_does_not_select_subscription(self):
        r=await self.service.send('user','我要退款');self.assertIsNone(r['commerce_case']);self.assertGreater(len(r['candidates']),1)
    async def test_goods_policy_uses_goods_sources(self):
        r=await self.service.send('user','商品退款政策是什么');self.assertEqual(r['route'],'knowledge')
        self.assertTrue(r['sources']);self.assertTrue(all(d['domain']!='subscription' for d in r['sources']))
    async def test_subscription_policy_uses_subscription_sources(self):
        r=await self.service.send('user','会员费退款政策是什么');self.assertEqual(r['route'],'knowledge')
        self.assertTrue(r['sources']);self.assertTrue(all(d['domain']!='goods' for d in r['sources']))

if __name__ == '__main__':unittest.main()

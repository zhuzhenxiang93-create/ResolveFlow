"""Concrete money, lifecycle, isolation and multi-turn acceptance scenarios."""
import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from business.commerce import CommerceStore, DAY
from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from agents.chat_tools import build_shared_rag_tool
from mcp.tool_manager import ToolResult


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CommerceStore(self.tmp.name+'/state.db')
        self.catalog = self.s.seed('alice')

    def obj(self, slug):
        return next(o for o in self.s.catalog('alice') if o['id'].endswith('-'+slug))

    def case(self, slug, op='refund', **args):
        return self.s.create_case('alice','conv',[dict(object_id=self.obj(slug)['id'],operation=op,**args)])

    def step(self,c,decision,actor=None):
        return self.s.transition('alice',c['id'],c['operations'][0]['id'],decision,actor or ('alice' if decision in ('confirm','cancel') else 'reviewer'))

    def complete(self,c):
        c=self.step(c,'confirm');c=self.step(c,'approve')
        if c['status']=='awaiting_return':c=self.step(c,'receive_return')
        return self.step(c,'settle')

    def test_goods_01_paid_not_shipped_full(self):
        c=self.complete(self.case('keyboard'));self.assertEqual(c['status'],'completed');self.assertEqual(self.obj('keyboard')['refunds'][0]['amount_minor'],39900)
    def test_goods_02_partial_discount_no_shipping(self):
        o=self.obj('keyboard');c=self.complete(self.case('keyboard',item_id=o['items'][1]['id']));self.assertEqual(c['operations'][0]['quote']['amount_minor'],9000)
    def test_goods_03_remaining_after_partial(self):
        self.complete(self.case('keyboard',item_id=self.obj('keyboard')['items'][1]['id']));self.assertEqual(self.case('keyboard')['operations'][0]['quote']['amount_minor'],30900)
    def test_goods_04_unpaid_cancel(self):
        c=self.step(self.case('cable','cancel_order'),'confirm');self.assertEqual(self.obj('cable')['status'],'cancelled');self.assertFalse(self.obj('cable')['refunds'])
    def test_goods_05_unpaid_refund_rejected(self):
        self.assertEqual(self.case('cable')['operations'][0]['status'],'ineligible')
    def test_goods_06_shipped_wait_return(self):
        c=self.step(self.step(self.case('speaker'),'confirm'),'approve');self.assertEqual(c['status'],'awaiting_return')
        with self.assertRaises(ValueError):self.step(c,'settle')
    def test_goods_07_delivered_return_and_invoice(self):
        self.complete(self.case('headphones'));self.assertEqual(self.obj('headphones')['invoice']['status'],'adjustment_required')
    def test_goods_08_expired(self):
        self.assertEqual(self.case('old')['operations'][0]['status'],'ineligible')
    def test_goods_09_repeat_refund(self):
        self.complete(self.case('keyboard'));self.assertEqual(self.case('keyboard')['operations'][0]['status'],'ineligible')
    def test_goods_10_foreign_item(self):
        with self.assertRaises(ValueError):self.case('keyboard',item_id=self.obj('headphones')['items'][0]['id'])
    def test_goods_11_foreign_owner(self):
        with self.assertRaises(ValueError):self.s.detail('bob',self.obj('keyboard')['id'])
    def test_goods_12_cancel_paid_not_allowed(self):
        self.assertEqual(self.case('keyboard','cancel_order')['operations'][0]['status'],'ineligible')
    def test_goods_13_approval_reserves_amount(self):
        c=self.step(self.step(self.case('keyboard'),'confirm'),'approve');self.assertEqual(self.case('keyboard')['operations'][0]['status'],'ineligible')
    def test_goods_14_failed_payment_releases(self):
        c=self.step(self.step(self.case('keyboard'),'confirm'),'approve');self.step(c,'fail');self.assertEqual(self.case('keyboard')['operations'][0]['quote']['amount_minor'],39900)
    def test_goods_15_duplicate_settlement_idempotent(self):
        c=self.complete(self.case('keyboard'));again=self.step(c,'settle');self.assertEqual(again,c);self.assertEqual(len(self.obj('keyboard')['refunds']),1)
    def test_goods_16_logistics_actual_events(self):
        self.assertEqual(self.obj('speaker')['shipment']['events'][-1]['status'],'shipped')
    def test_subscription_01_duplicate_amount(self):
        c=self.complete(self.case('pro'));self.assertEqual(c['operations'][0]['quote']['amount_minor'],9900);self.assertTrue(self.obj('pro')['auto_renew'])
    def test_subscription_02_unused_renewal_refund(self):
        self.complete(self.case('basic'));self.assertEqual(self.obj('basic')['status'],'terminated');self.assertFalse(self.obj('basic')['auto_renew'])
    def test_subscription_03_used_rejected(self):
        self.assertEqual(self.case('used')['operations'][0]['status'],'ineligible')
    def test_subscription_04_cancel_renewal_preserves(self):
        before=self.obj('pro');self.step(self.case('pro','cancel_renewal'),'confirm');after=self.obj('pro');self.assertFalse(after['auto_renew']);self.assertEqual(after['entitlement'],before['entitlement']);self.assertFalse(after['refunds'])
    def test_subscription_05_terminate_no_refund(self):
        self.step(self.case('pro','terminate'),'confirm');self.assertEqual(self.obj('pro')['entitlement'],'none');self.assertFalse(self.obj('pro')['refunds'])
    def test_subscription_06_repair(self):
        self.step(self.case('pro','repair'),'confirm');self.assertEqual(self.obj('pro')['entitlement'],self.obj('pro')['plan'])
    def test_subscription_07_no_self_review(self):
        c=self.step(self.case('pro'),'confirm')
        with self.assertRaises(ValueError):self.step(c,'approve','alice')
    def test_subscription_08_no_confirm_no_review(self):
        with self.assertRaises(ValueError):self.step(self.case('pro'),'approve')
    def test_subscription_09_reject_no_money(self):
        c=self.step(self.case('pro'),'confirm');self.step(c,'reject');self.assertFalse(self.obj('pro')['refunds'])
    def test_subscription_10_user_cancel_no_money(self):
        self.step(self.case('pro'),'cancel');self.assertFalse(self.obj('pro')['refunds'])
    def test_subscription_11_specific_initial_bill(self):
        o=self.obj('pro');c=self.complete(self.case('pro',payment_id=o['id']+'-P1'));self.assertEqual(self.obj('pro')['status'],'terminated')
    def test_subscription_12_foreign_payment(self):
        self.assertEqual(self.case('pro',payment_id=self.obj('basic')['id']+'-P1')['operations'][0]['status'],'ineligible')
    def test_subscription_13_approval_expiry(self):
        c=self.step(self.case('pro'),'confirm');c['operations'][0]['expires_at']=0
        with self.s.connect() as db:self.s._save(db,c)
        self.assertEqual(self.step(c,'approve')['operations'][0]['status'],'stale');self.assertFalse(self.obj('pro')['refunds'])
    def test_subscription_14_state_changed(self):
        c=self.step(self.case('pro'),'confirm');self.step(self.case('pro','cancel_renewal'),'confirm');self.assertEqual(self.step(c,'approve')['operations'][0]['status'],'stale')
    def test_subscription_15_restart_and_idempotency(self):
        c=self.step(self.case('pro'),'confirm');self.s=CommerceStore(self.tmp.name+'/state.db');c=self.step(c,'approve');again=self.step(c,'approve');self.assertEqual(c,again)
    def test_subscription_16_wrong_operation_on_goods(self):
        with self.assertRaises(ValueError):self.case('keyboard','terminate')
    def test_mixed_independent_objects(self):
        c=self.s.create_case('alice','c',[{'object_id':self.obj('headphones')['id'],'operation':'refund'},{'object_id':self.obj('basic')['id'],'operation':'cancel_renewal'}]);self.s.transition('alice',c['id'],c['operations'][1]['id'],'confirm','alice');c=self.s.get_case('alice',c['id']);self.assertEqual(c['operations'][0]['status'],'awaiting_confirmation');self.assertFalse(self.obj('basic')['auto_renew'])
    def test_seed_never_resets_user_changes(self):
        self.step(self.case('basic','cancel_renewal'),'confirm');self.s.seed('alice');self.assertFalse(self.obj('basic')['auto_renew']);self.assertEqual(len(self.s.catalog('alice')),8)


class ConversationAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=ActionRuntime(self.tmp.name+'/db');self.s=ConversationService(self.r);self.db=CommerceStore(self.r.path);self.rows=self.db.seed('alice')
    async def test_ambiguous_then_select(self):
        r=await self.s.send('alice','我要退款');self.assertTrue(r['candidates']);o=next(o for o in self.rows if o['id'].endswith('headphones'));r=await self.s.send('alice',o['id'],conversation_id=r['conversation_id']);self.assertEqual(r['commerce_case']['status'],'awaiting_confirmation')
    async def test_mixed_no_dropped_goal(self):
        r=await self.s.send('alice','无线耳机退掉，Basic 月度会员也别续了');self.assertEqual(len(r['commerce_case']['operations']),2)
    async def test_negation_read_only(self):
        r=await self.s.send('alice','无线耳机只查物流，不退款');self.assertIsNone(r['commerce_case']);self.assertIn('DEMO-headphones',r['response'])
    async def test_profile_update_delete_isolation(self):
        await self.s.memory.set_profile('alice',{'response_style':'concise'});self.assertEqual(await self.s.memory.get_profile('bob'),{});await self.s.memory.forget('alice');self.assertEqual(await self.s.memory.get_profile('alice'),{})
    async def test_memory_style_changes_answer(self):
        o=next(o for o in self.rows if o['id'].endswith('pro'));a=await self.s.send('alice','查询 '+o['id']);await self.s.memory.set_profile('alice',{'response_style':'concise'});b=await self.s.send('alice','查询 '+o['id']);self.assertLess(len(b['response']),len(a['response']));self.assertIn('990',str(o['payments'][0]['amount_minor']))
    async def test_cross_session_reference(self):
        o=next(o for o in self.rows if o['id'].endswith('headphones'));await self.s.send('alice','查询 '+o['id']);r=await self.s.send('alice','上次那个耳机订单查一下');self.assertIn(o['title'],r['response'])
    async def test_revision_cancels_goods_only(self):
        r=await self.s.send('alice','无线耳机退掉');c=r['commerce_case'];await self.s.send('alice','我改主意了，商品不退了，只取消续费',conversation_id=r['conversation_id']);self.assertEqual(self.db.get_case('alice',c['id'])['operations'][0]['status'],'cancelled')
    async def test_toolresult_serializable_and_failure(self):
        manager=SimpleNamespace(search_with_rewrite=AsyncMock(return_value=ToolResult(True,[{'document_id':'x','title':'rule'}],'knowledge_search')))
        tool=build_shared_rag_tool(manager);out=await tool.handler(SimpleNamespace(message='退款'),{});json.dumps(out);self.assertEqual(out['sources'][0]['document_id'],'x');manager.search_with_rewrite.return_value=ToolResult(False,[],'knowledge_search',error='down');self.assertFalse((await tool.handler(SimpleNamespace(message='退款'),{}))['success'])

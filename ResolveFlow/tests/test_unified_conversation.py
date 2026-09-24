"""Unified public conversation contract after retiring counter-based execution.
Security assertions retained against object-bound commerce operations."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from business.commerce import CommerceStore
from business.conversation import CommerceConversation, Proposal, Intent
from memory.conversation_memory import MsgRole

class UnifiedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.r=ActionRuntime(self.temp.name+'/db');self.s=ConversationService(self.r)
        self.store=CommerceStore(self.r.path);self.rows=self.store.seed('user')
        self.order=next(o for o in self.rows if o['id'].endswith('basic'))
    async def pending(self):return await self.s.send('user','关闭自动续费 '+self.order['id'])
    async def test_consult_then_action_shared_memory_and_no_implicit_confirmation(self):
        first=await self.s.send('user','怎么退订');self.assertTrue(first['knowledge_used'])
        second=await self.s.send('user','那就帮我关掉',conversation_id=first['conversation_id'])
        self.assertEqual(second['status'],'awaiting_clarification')
        third=await self.s.send('user',self.order['id'],conversation_id=first['conversation_id'])
        case=third['commerce_case'];self.assertEqual(case['status'],'awaiting_confirmation')
        before=self.store.get_case('user',case['id'])
        await self.s.send('user','好的',conversation_id=first['conversation_id'])
        self.assertEqual(self.store.get_case('user',case['id']),before)
        done=self.store.transition('user',case['id'],case['operations'][0]['id'],'confirm','user')
        self.assertEqual(done['status'],'completed');self.assertFalse(self.store.detail('user',self.order['id'])['auto_renew'])
        self.assertTrue((await self.s.memory.get_context('user',first['conversation_id'])).recent_messages)
    async def test_conversation_owner_and_profile_isolation(self):
        result=await self.s.send('user','请简洁回答，退订政策');conv=result['conversation_id']
        with self.assertRaises(ValueError):await self.s.send('intruder','查询',conversation_id=conv)
        self.assertEqual((await self.s.memory.get_context('user',conv)).user_profile['response_style'],'concise')
        other=await self.s.memory.get_context('intruder','other');self.assertEqual(other.user_profile,{});self.assertEqual(other.relevant_history,[])
    async def test_restart_and_history_retrieval(self):
        first=await self.s.send('user','怎么退订');restarted=ConversationService(ActionRuntime(self.r.path))
        self.assertGreaterEqual(len((await restarted.memory.get_context('user',first['conversation_id'])).recent_messages),2)
        self.assertTrue((await restarted.memory.get_context('user','new','退订')).relevant_history)
    async def test_full_knowledge_adapter_and_unavailable_fallback(self):
        self.s.knowledge_search=AsyncMock(return_value=[{'document_id':'subscription-commerce-plan','title':'规则','content':'内部已核实规则','lexical_rank':1}])
        result=await self.s.send('user','退订政策');self.s.knowledge_search.assert_awaited_once();self.assertIn('内部已核实规则',result['response'])
        self.s.knowledge_search.side_effect=RuntimeError()
        failed=await self.s.send('user','退订政策');self.assertIn('full_rag_unavailable',failed['degradations']);self.assertTrue(failed['sources'])
    async def test_memory_failure_does_not_allow_write(self):
        self.s.memory.get_context=AsyncMock(side_effect=RuntimeError())
        result=await self.s.send('user','关闭自动续费')
        self.assertEqual(result['status'],'awaiting_clarification');self.assertIsNone(result['commerce_case'])
        self.assertTrue(self.store.detail('user',self.order['id'])['auto_renew'])
    async def test_credential_memory_redaction(self):
        await self.s.memory.add_message('user','c',MsgRole.USER,'密码: fake-secret sk-fakeapikey123')
        text=(await self.s.memory.get_context('user','c')).to_prompt_text();self.assertNotIn('fake-secret',text);self.assertNotIn('sk-fake',text)
    async def test_concurrent_turn_is_rejected(self):
        conv,_,_=self.s._conversation('user');entered,proceed=asyncio.Event(),asyncio.Event()
        original=CommerceConversation.interpret
        async def held(instance,*args):entered.set();await proceed.wait();return await original(instance,*args)
        with patch.object(CommerceConversation,'interpret',held):
            first=asyncio.create_task(self.s.send('user','退订政策',conversation_id=conv))
            await asyncio.wait_for(entered.wait(),2)
            try:
                with self.assertRaises(ValueError):await self.s.send('user','关闭续费',conversation_id=conv)
            finally:proceed.set();await first
    async def test_agent_orchestrator_failure_diagnostic_is_queryable(self):
        self.s.answer_orchestrator=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(response='暂时失败',tool_traces=[],error_diagnostics=[{'error_type':'APIConnectionError'}])))
        r=await self.s.send('user','你好');self.assertEqual(r['response'],'暂时失败');self.assertIn('agent_call_failed',r['degradations']);self.assertEqual(r['interpretation']['diagnostics'][0]['error_type'],'APIConnectionError')
    async def test_intent_timeout_preserves_task_and_releases_lease(self):
        first=await self.pending();before=first['commerce_case'];self.s.recognizer.recognize=AsyncMock(side_effect=asyncio.TimeoutError())
        r=await self.s.send('user','你好',conversation_id='unrelated');self.assertEqual(r['status'],'retryable_error');self.assertEqual(self.store.get_case('user',before['id']),before)
        with self.r.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM unified_leases').fetchone()[0],0)
    async def test_goal_failure_never_falls_back_to_old_execution(self):
        self.r.client=AsyncMock();self.r.client.create_tool_turn.side_effect=asyncio.TimeoutError()
        self.r.advance=AsyncMock(side_effect=AssertionError('legacy'))
        r=await self.s.send('user','关闭自动续费 '+self.order['id']);self.assertEqual(r['interpretation']['mode'],'model_error');self.assertIsNone(r['commerce_case']);self.r.advance.assert_not_awaited()
    async def test_pending_side_policy_cannot_inherit_write_goal(self):
        started=await self.pending();before=started['commerce_case']
        proposal=Proposal(intents=[Intent(domain='subscription',operation='refund',reference=self.order['id'])])
        with patch.object(CommerceConversation,'interpret',AsyncMock(return_value=(proposal,'mock'))):
            for question in ['顺便解释一下退款政策，我现在不申请退款。','介绍退款规则','请说明退订流程']:
                r=await self.s.send('user',question,conversation_id=started['conversation_id']);self.assertEqual(r['status'],'awaiting_clarification' if '退款' in question else 'answered');self.assertIsNone(r['commerce_case']);self.assertEqual(self.store.get_case('user',before['id']),before)
    async def test_technical_remainder_preserves_business_answer(self):
        self.s.answer_orchestrator=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(response='登录401排查说明',tool_traces=[])))
        r=await self.s.send('user','关闭自动续费 '+self.order['id']+'，另外登录报错401')
        self.assertIn('登录401排查说明',r['response']);self.assertIn('待用户确认',r['response']);self.s.answer_orchestrator.run.assert_awaited_once()
    async def test_single_business_request_skips_support(self):
        self.s.answer_orchestrator=SimpleNamespace(run=AsyncMock());await self.pending();self.s.answer_orchestrator.run.assert_not_awaited()
    async def test_chat_and_conversation_api_share_confirmation_and_memory(self):
        import httpx,os
        from api import main,action_routes,commerce_routes
        from core.auth import mint_token
        with patch.dict(os.environ,{'AGENT_JWT_SECRET':'unit'}),patch.object(action_routes,'conversation_service',return_value=self.s),patch.object(main,'_configure_conversation_service',return_value=self.s),patch.object(commerce_routes,'runtime',return_value=self.r),patch.object(commerce_routes,'conversation_service',return_value=self.s):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test',headers={'Authorization':'Bearer '+mint_token('user','user',3600)}) as c:
                first=(await c.post('/chat',json={'message':'怎么退订'})).json()
                second=await c.post('/agent/conversation',json={'message':'取消订阅 '+self.order['id'],'conversation_id':first['conv_id']})
                case=second.json()['commerce_case'];a=case['operations'][0]
                cancel=await c.post('/commerce/cases/'+case['id']+'/decision',json={'action_id':a['id'],'decision':'cancel'})
                self.assertEqual(cancel.status_code,200)
                self.assertEqual((await self.s.memory.get_context('user',first['conv_id'])).recent_messages[-1].content,cancel.json()['response'])
                self.assertEqual((await c.post('/agent/conversation',json={'message':'查询'},headers={'Authorization':'Bearer wrong'})).status_code,401)

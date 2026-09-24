"""Active application contract: legacy data never selects a second executor."""
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from business.commerce import CommerceStore
from business.execution import ExecutionContext
from api import main, action_routes, commerce_routes
from core.auth import mint_token

class ConvergenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=ActionRuntime(self.tmp.name+'/db');self.old=self.r.seed('old')
        self.store=CommerceStore(self.r.path);self.s=ConversationService(self.r)
    async def test_missing_catalog_never_falls_back_to_old_executor(self):
        self.r.advance=AsyncMock(side_effect=AssertionError('old engine called'))
        for user in ['empty','old','full']:
            if user=='full':self.store.seed(user)
            r=await self.s.send(user,'我要退款')
            self.assertEqual(r['route'],'commerce')
            self.assertIsNone(r['task'])
        self.r.advance.assert_not_awaited()
    async def test_historical_refund_needs_evidence_not_counters(self):
        r=await self.s.send('old','申请退款 '+self.old['id'])
        a=r['commerce_case']['operations'][0]
        self.assertEqual(a['status'],'ineligible');self.assertIn('账单',a['quote']['reason'])
        self.assertFalse(self.store.detail('old',self.old['id'])['payments'])
        with self.r.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
    async def test_catalog_presence_does_not_change_policy(self):
        before=await self.s.send('old','会员费退款政策是什么')
        self.store.seed('old')
        after=await self.s.send('old','会员费退款政策是什么')
        self.assertEqual(before['response'],after['response']);self.assertEqual(before['sources'],after['sources'])
    async def test_cancellation_synonyms_preserve_paid_benefits(self):
        for index,verb in enumerate(['退订','取消订阅','关闭自动续费','立即终止订阅']):
            user='u'+str(index);obj=next(o for o in self.store.seed(user) if o['id'].endswith('basic'))
            r=await self.s.send(user,verb+' '+obj['id'])
            case=r['commerce_case'];a=case['operations'][0]
            self.assertEqual(a['quote']['operation'],'cancel_renewal')
            self.store.transition(user,case['id'],a['id'],'confirm',user)
            after=self.store.detail(user,obj['id'])
            self.assertFalse(after['auto_renew']);self.assertEqual(after['entitlement'],obj['entitlement'])
            self.assertEqual(after['period_end'],obj['period_end']);self.assertFalse(after['refunds'])
    async def test_unified_cli_never_uses_old_confirmation(self):
        from scripts.agent_cli import Session
        session=Session(self.r,owner='cli',unified=True)
        rows=(await session.handle('/seed'))['objects'];obj=next(o for o in rows if o['id'].endswith('basic'))
        result=await session.handle('取消订阅 '+obj['id']);case=result['commerce_case']
        done=await session.handle('/confirm '+case['operations'][0]['id'])
        self.assertEqual(done['operations'][0]['status'],'completed')
        self.assertFalse(self.store.detail('cli',obj['id'])['auto_renew'])
    async def test_legacy_task_is_read_only_and_old_consent_not_transferred(self):
        task=self.r.create('old','关闭自动续费','old-conv')
        archived=ExecutionContext(self.r.path).get(task['id'],'old')
        self.assertTrue(archived['read_only'])
        with self.assertRaises(ValueError):ExecutionContext(self.r.path).get(task['id'],'other')
        result=await self.s.send('old','申请退款 '+self.old['id'],task_id=task['id'])
        self.assertEqual(result['commerce_case']['operations'][0]['status'],'ineligible')
        self.assertEqual(self.r.get(task['id'],'old'),task)

    async def test_ambiguous_policy_clarifies_domain_without_creating_case(self):
        for user in ['empty','old','full']:
            if user=='full':self.store.seed(user)
            first=await self.s.send(user,'退款有什么条件？')
            self.assertEqual(first['status'],'awaiting_clarification')
            self.assertIsNone(first['commerce_case']);self.assertIn('商品',first['response']);self.assertIn('订阅',first['response'])
            second=await self.s.send(user,'订阅',conversation_id=first['conversation_id'])
            self.assertEqual(second['route'],'knowledge');self.assertIsNone(second['commerce_case'])
            self.assertTrue(second['sources']);self.assertFalse(self.store.cases(user))
    async def test_one_legacy_record_does_not_imply_refund_target(self):
        result=await self.s.send('old','我要退款')
        self.assertIsNone(result['commerce_case']);self.assertEqual(result['status'],'awaiting_clarification')
        self.assertEqual(len(result['candidates']),1)
    async def test_model_cannot_choose_target_for_ambiguous_request(self):
        from business.conversation import CommerceConversation,Proposal,Intent
        self.store.seed('full');obj=next(o for o in self.store.catalog('full') if o['id'].endswith('pro'))
        proposal=Proposal(intents=[Intent(domain='subscription',operation='refund',reference=obj['id'])])
        with patch.object(CommerceConversation,'interpret',AsyncMock(return_value=(proposal,'mock'))):
            result=await self.s.send('full','我要退款')
        self.assertIsNone(result['commerce_case']);self.assertEqual(result['status'],'awaiting_clarification')

        self.assertEqual({o['domain'] for o in result['candidates']}, {'goods', 'subscription'})

class ConvergenceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=ActionRuntime(self.tmp.name+'/db');self.s=ConversationService(self.r)
        for p in [patch.dict('os.environ',{'AGENT_JWT_SECRET':'test-convergence'}),patch.object(action_routes,'runtime',return_value=self.r),patch.object(action_routes,'conversation_service',return_value=self.s),patch.object(commerce_routes,'runtime',return_value=self.r),patch.object(commerce_routes,'conversation_service',return_value=self.s),patch.object(main,'_configure_conversation_service',return_value=self.s)]:
            p.start();self.addCleanup(p.stop)
        self.c=TestClient(main.app);self.addCleanup(self.c.close)
        self.u={'Authorization':'Bearer '+mint_token('alice','user',3600)}
        self.rv={'Authorization':'Bearer '+mint_token('reviewer','reviewer',3600)}
    def test_all_start_endpoints_share_engine_and_object_policy(self):
        rows=self.c.post('/agent/demo/orders',headers=self.u).json()['objects'];oid=next(o['id'] for o in rows if o['id'].endswith('basic'))
        quotes=[]
        for endpoint in ['/chat','/agent/conversation','/agent/tasks']:
            r=self.c.post(endpoint,headers=self.u,json={'message':'取消订阅 '+oid})
            self.assertEqual(r.status_code,200)
            quotes.append(r.json()['commerce_case']['operations'][0]['quote'])
        self.assertEqual(quotes[0],quotes[1]);self.assertEqual(quotes[1],quotes[2])
    def test_old_mutating_endpoints_cannot_execute(self):
        old=self.r.seed('alice');task=self.r.create('alice','退款')
        endpoints={'confirmation':{'confirmation_id':'old','accepted':True},'continue':{},'revise':{'message':'退款'},'cancel':{},'approval':{'approval_id':'old','approved':True},'release':{}}
        for suffix,body in endpoints.items():
            response=self.c.post('/agent/tasks/'+task['id']+'/'+suffix,headers=self.rv if suffix in ['approval','release'] else self.u,json=body)
            self.assertEqual(response.status_code,410)
        self.assertEqual(self.r.get(task['id'],'alice'),task)
        self.assertTrue(self.c.get('/agent/tasks/'+task['id'],headers=self.u).json()['read_only'])

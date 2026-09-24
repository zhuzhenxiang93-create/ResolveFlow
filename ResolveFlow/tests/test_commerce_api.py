import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from business.commerce import CommerceStore
from core.auth import mint_token
from api import main, action_routes, commerce_routes

class CommerceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=ActionRuntime(self.tmp.name+'/db');self.s=ConversationService(self.r)
        self.patches=[patch.dict(os.environ,{'AGENT_JWT_SECRET':'test-commerce-only'}),patch.object(action_routes,'runtime',return_value=self.r),patch.object(action_routes,'conversation_service',return_value=self.s),patch.object(commerce_routes,'runtime',return_value=self.r),patch.object(commerce_routes,'conversation_service',return_value=self.s),patch.object(main,'_configure_conversation_service',return_value=self.s)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.c=TestClient(main.app);self.addCleanup(self.c.close)
        self.user={'Authorization':'Bearer '+mint_token('alice','user',3600)}
        self.other={'Authorization':'Bearer '+mint_token('bob','user',3600)}
        self.reviewer={'Authorization':'Bearer '+mint_token('reviewer','reviewer',3600)}
        self.rows=self.c.post('/commerce/demo',headers=self.user).json()['objects']
    def test_identity_not_payload_and_review_role(self):
        oid=next(o['id'] for o in self.rows if o['id'].endswith('pro'))
        r=self.c.post('/chat',headers=self.user,json={'message':'申请退款 '+oid,'user_id':'bob'}).json()
        case=r['commerce_case'];a=case['operations'][0]
        self.assertEqual(case['owner'],'alice')
        path='/commerce/cases/'+case['id']+'/decision'
        self.assertEqual(self.c.post(path,headers=self.other,json={'action_id':a['id'],'decision':'confirm'}).status_code,400)
        self.assertEqual(self.c.post(path,headers=self.user,json={'action_id':a['id'],'decision':'confirm'}).status_code,200)
        self.assertEqual(self.c.post('/commerce/review/'+case['id'],headers=self.user,json={'action_id':a['id'],'decision':'approve'}).status_code,403)
        self.assertEqual(self.c.post('/commerce/review/'+case['id'],headers=self.reviewer,json={'action_id':a['id'],'decision':'approve'}).status_code,200)
        self.assertEqual(self.c.get('/commerce/objects/'+oid,headers=self.other).status_code,404)
    def test_profile_correct_delete_is_authenticated(self):
        self.assertEqual(self.c.put('/commerce/memory',json={'response_style':'concise'}).status_code,401)
        self.assertEqual(self.c.put('/commerce/memory',headers=self.user,json={'response_style':'concise'}).status_code,200)
        self.assertEqual(self.c.get('/commerce/memory',headers=self.other).json()['profile'],{})
        self.c.delete('/commerce/memory',headers=self.user)
        self.assertEqual(self.c.get('/commerce/memory',headers=self.user).json()['profile'],{})
    def test_concurrent_approval_reserves_once(self):
        store=CommerceStore(self.r.path);oid=next(o['id'] for o in self.rows if o['id'].endswith('keyboard'))
        c=store.create_case('alice','c',[{'object_id':oid,'operation':'refund'}]);aid=c['operations'][0]['id'];store.transition('alice',c['id'],aid,'confirm','alice')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:store.transition('alice',c['id'],aid,'approve','reviewer'),range(2)))
        self.assertEqual(len(store.detail('alice',oid)['refunds']),1)
        self.assertTrue(all(r['status']=='refund_processing' for r in results))

    def test_memory_failure_does_not_hide_committed_decision(self):
        store=CommerceStore(self.r.path)
        oid=next(o['id'] for o in self.rows if o['id'].endswith('basic'))
        case=store.create_case('alice','c',[{'object_id':oid,'operation':'cancel_renewal'}])
        with patch.object(self.s.memory,'add_message',new=AsyncMock(side_effect=RuntimeError('memory unavailable'))):
            response=self.c.post('/commerce/cases/'+case['id']+'/decision',headers=self.user,json={'action_id':case['operations'][0]['id'],'decision':'confirm'})
        self.assertEqual(response.status_code,200)
        self.assertIn('memory_write_failed',response.json()['degradations'])
        self.assertFalse(store.detail('alice',oid)['auto_renew'])

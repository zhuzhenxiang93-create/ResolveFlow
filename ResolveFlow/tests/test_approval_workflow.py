"""Independent review safety on the sole active commerce lifecycle."""
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from api.action_demo import app
from api.action_routes import runtime
from tests.action_test_support import mint_token
from business.commerce import CommerceStore

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict('os.environ',{'AGENT_STATE_PATH':self.temp.name+'/db','AGENT_USE_LLM':'0','AGENT_JWT_SECRET':'test-signing-secret'})
        env.start();self.addCleanup(env.stop);runtime.cache_clear();self.addCleanup(runtime.cache_clear)
        self.client=TestClient(app);self.addCleanup(self.client.close)
        self.user={'Authorization':'Bearer '+mint_token('user','alice')};self.review={'Authorization':'Bearer '+mint_token('reviewer','bob')}
    def pending(self):
        rows=self.client.post('/agent/demo/orders',headers=self.user).json()['objects'];self.oid=next(o['id'] for o in rows if o['id'].endswith('pro'))
        task=self.client.post('/agent/tasks',headers=self.user,json={'message':'申请重复扣款退款 '+self.oid}).json()['commerce_case']
        self.aid=task['operations'][0]['id'];base='/commerce/cases/'+task['id']
        task=self.client.post(base+'/decision',headers=self.user,json={'action_id':self.aid,'decision':'confirm'}).json()
        self.assertEqual(task['status'],'awaiting_approval');return task,'/commerce/review/'+task['id']
    def decide(self,base,decision,headers=None):return self.client.post(base,headers=headers or self.review,json={'action_id':self.aid,'decision':decision})
    def test_independent_review_approve_replay_and_shared_state(self):
        task,base=self.pending()
        self.assertEqual(self.client.get('/agent/review/tasks',headers=self.user).status_code,403)
        self.assertEqual(self.client.get('/agent/review/tasks').status_code,403)
        self.assertIn(task['id'],[t['id'] for t in self.client.get('/agent/review/tasks',headers=self.review).json()['tasks']])
        self.assertEqual(self.decide(base,'approve',self.user).status_code,403)
        self.assertEqual(self.decide(base,'approve').json()['status'],'refund_processing')
        self.assertEqual(self.decide(base,'approve').json()['status'],'refund_processing')
        self.assertEqual(self.decide(base,'settle').json()['status'],'completed')
        self.decide(base,'settle');self.assertEqual(len(CommerceStore(runtime().path).detail('alice',self.oid)['refunds']),1)
        runtime.cache_clear();self.assertEqual(self.client.get('/agent/tasks/'+task['id'],headers=self.user).json()['status'],'completed')
    def test_rejection_does_not_refund(self):
        task,base=self.pending();result=self.decide(base,'reject').json()
        self.assertEqual(result['operations'][0]['status'],'rejected');self.assertFalse(CommerceStore(runtime().path).detail('alice',self.oid)['refunds'])
    def test_invalidated_release_requires_new_consent(self):
        task,base=self.pending();old_aid=self.aid
        self.client.post('/commerce/cases/'+task['id']+'/decision',headers=self.user,json={'action_id':self.aid,'decision':'cancel'})
        self.assertEqual(self.decide(base,'approve').status_code,400)
        self.assertEqual(self.client.post('/agent/tasks/'+task['id']+'/release',headers=self.user).status_code,403)
        self.assertEqual(self.client.post('/agent/tasks/'+task['id']+'/release',headers=self.review).status_code,410)
        new=self.client.post('/agent/tasks',headers=self.user,json={'message':'申请退款 '+self.oid}).json()['commerce_case']
        self.assertNotEqual(new['operations'][0]['id'],old_aid)
        self.assertEqual(self.decide('/commerce/review/'+new['id'],'approve').status_code,400)
        self.assertFalse(CommerceStore(runtime().path).detail('alice',self.oid)['refunds'])
    def test_expired_approval_cannot_execute(self):
        task,base=self.pending()
        with patch('business.commerce.time.time',return_value=task['operations'][0]['expires_at']+1):result=self.decide(base,'approve').json()
        self.assertEqual(result['operations'][0]['status'],'stale');self.assertFalse(CommerceStore(runtime().path).detail('alice',self.oid)['refunds'])

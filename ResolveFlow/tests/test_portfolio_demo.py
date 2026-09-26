"""Isolated demo host: real API, auth and CommerceStore, no model stubs."""
import os
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from api import commerce_routes
from api.portfolio_demo import create_app
from core.auth import decode_token

class PortfolioDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'RESOLVEFLOW_DEMO_MODE':'true','RESOLVEFLOW_DEMO_DIR':self.tmp.name,'AGENT_USE_LLM':'0'})
        self.env.start()
        self.old=commerce_routes.runtime,commerce_routes.conversation_service
        self.client=TestClient(create_app())
        self.session=self.client.post('/demo/session').json()
        self.user={'Authorization':'Bearer '+self.session['userToken']}
        self.reviewer={'Authorization':'Bearer '+self.session['reviewerToken']}
    def tearDown(self):
        self.client.close()
        commerce_routes.runtime,commerce_routes.conversation_service=self.old
        self.env.stop();self.tmp.cleanup()
    def chat(self,text):
        r=self.client.post('/chat',headers=self.user,json={'message':text})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()
    def decide(self,c,a,d,review=False,headers=None):
        url=f"/commerce/review/{c['id']}" if review else f"/commerce/cases/{c['id']}/decision"
        return self.client.post(url,headers=headers or (self.reviewer if review else self.user),json={'action_id':a['id'],'decision':d})
    def test_policy_and_ambiguity(self):
        p=self.chat('商品退款政策是什么？')
        self.assertTrue(p['sources']);self.assertIsNone(p['commerce_case'])
        r=self.chat('我要退款。')
        self.assertEqual(len(r['candidates']),8);self.assertIsNone(r['commerce_case'])
    def test_refund_authority_idempotency_and_recovery(self):
        c=self.chat('帮我把无线耳机退掉。')['commerce_case'];a=c['operations'][0]
        self.assertEqual(a['quote']['amount_minor'],25900)
        self.assertEqual(self.decide(c,a,'approve',True).status_code,400)
        self.assertEqual(self.decide(c,a,'confirm').json()['operations'][0]['status'],'awaiting_approval')
        self.assertEqual(self.decide(c,a,'approve',True,headers=self.user).status_code,403)
        for d,status in [('approve','awaiting_return'),('receive_return','refund_processing'),('settle','completed')]:
            self.assertEqual(self.decide(c,a,d,True).json()['operations'][0]['status'],status)
        self.assertEqual(self.decide(c,a,'settle',True).json()['operations'][0]['status'],'completed')
        objects=self.client.get('/commerce/objects',headers=self.user).json()['objects']
        self.assertEqual(sum(len(o['refunds']) for o in objects),1)
        self.assertEqual(self.client.get('/commerce/cases',headers=self.user).json()['cases'][0]['operations'][0]['status'],'completed')
    def test_cancel_renewal_preserves_benefits(self):
        c=self.chat('Basic 会员下个月别续了。')['commerce_case'];a=c['operations'][0]
        url='/commerce/objects/'+a['quote']['object_id']
        old=self.client.get(url,headers=self.user).json()
        self.assertEqual(self.decide(c,a,'confirm').json()['operations'][0]['status'],'completed')
        new=self.client.get(url,headers=self.user).json()
        self.assertFalse(new['auto_renew']);self.assertEqual(new['entitlement'],old['entitlement']);self.assertEqual(new['refunds'],[])
    def test_reset_and_isolation(self):
        other=self.client.post('/demo/session').json();h={'Authorization':'Bearer '+other['userToken']}
        self.assertNotEqual(decode_token(self.session['userToken'])['sub'],decode_token(self.session['reviewerToken'])['sub'])
        obj=self.client.get('/commerce/objects',headers=self.user).json()['objects'][0]
        self.assertEqual(self.client.get('/commerce/objects/'+obj['id'],headers=h).status_code,404)
        self.assertEqual(self.client.get('/commerce/cases',headers=h).json()['cases'],[])
        self.assertEqual(len(self.client.get('/commerce/objects',headers=h).json()['objects']),8)
    def test_mixed_offline_limitation_is_explicit(self):
        r=self.chat('把 Pro 会员重复扣的钱退掉，而且登录一直报 401。')
        self.assertEqual(r['commerce_case']['operations'][0]['quote']['amount_minor'],9900)
        self.assertIn('technical_agent_offline',r['degradations'])
    def test_opt_in_required(self):
        with patch.dict(os.environ,{'RESOLVEFLOW_DEMO_MODE':'false'}):
            with self.assertRaises(RuntimeError):create_app()

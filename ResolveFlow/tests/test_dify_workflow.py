import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from api import commerce_routes
from api.portfolio_demo import create_app
from agents.dify_workflow import DifyWorkflow

class DifyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ, {"RESOLVEFLOW_DEMO_MODE":"true", "RESOLVEFLOW_DEMO_DIR":self.tmp.name,
            "AGENT_USE_LLM":"0", "RESOLVEFLOW_USE_DIFY":"1", "DIFY_API_BASE":"https://api.dify.ai/v1", "DIFY_API_KEY":"test-only"})
        self.env.start();self.old=commerce_routes.runtime,commerce_routes.conversation_service
        self.outputs={"proposal":json.dumps({"intents":[]}),"policy_answer":"","support_answer":"","sources":"[]","tool_traces":"[]"}
        self.requests=[]
        def transport(req):
            self.requests.append(json.loads(req.content))
            return httpx.Response(200,json={"data":{"status":"succeeded","outputs":self.outputs}})
        self.transport=httpx.MockTransport(transport)
        actual_init=DifyWorkflow.__init__
        transport_obj=self.transport
        def init(obj): actual_init(obj,transport=transport_obj)
        self.patch=patch.object(DifyWorkflow,"__init__",init);self.patch.start()
        self.client=TestClient(create_app());self.session=self.client.post('/demo/session').json()
        self.user={"Authorization":"Bearer "+self.session['userToken']}
        self.reviewer={"Authorization":"Bearer "+self.session['reviewerToken']}
    def tearDown(self):
        self.client.close();self.patch.stop();commerce_routes.runtime,commerce_routes.conversation_service=self.old
        self.env.stop();self.tmp.cleanup()
    def proposal(self,domain,op,reference="",**extra):
        self.outputs["proposal"]=json.dumps({"intents":[{"domain":domain,"operation":op,"reference":reference}],**extra})
    def chat(self,message,**extra):
        r=self.client.post('/chat',headers=self.user,json={"message":message,**extra})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_mixed_refund_confirmation_review_and_idempotency(self):
        self.proposal('subscription','refund','Pro',general_remainder='登录报401')
        self.outputs['support_answer']='401 可能是凭据过期，请重新登录；没有访问实际账号。'
        self.outputs['tool_traces']=json.dumps([{"tool":"lookup_error_code","arguments":{"code":"401"}}])
        result=self.chat('把 Pro 会员重复扣的钱退掉，而且登录一直报 401')
        self.assertEqual(result['mode'],'dify_workflow');self.assertIn('401',result['response'])
        self.assertEqual(result['support_result']['tools_used'],['lookup_error_code'])
        c=result['commerce_case'];a=c['operations'][0]
        self.assertEqual(a['quote']['amount_minor'],9900);self.assertEqual(a['status'],'awaiting_confirmation')
        self.assertNotIn('technical_agent_offline',result['degradations'])
        u=f"/commerce/cases/{c['id']}/decision";v=f"/commerce/review/{c['id']}"
        self.assertEqual(self.client.post(v,headers=self.user,json={'action_id':a['id'],'decision':'approve'}).status_code,403)
        self.client.post(u,headers=self.user,json={'action_id':a['id'],'decision':'confirm'})
        for d in ('approve','settle','settle'):
            res=self.client.post(v,headers=self.reviewer,json={'action_id':a['id'],'decision':d})
            self.assertEqual(res.status_code,200,res.text)
        objs=self.client.get('/commerce/objects',headers=self.user).json()['objects']
        self.assertEqual(sum(len(o['refunds']) for o in objs),1)
        self.assertEqual(self.client.get('/commerce/cases',headers=self.user).json()['cases'][0]['operations'][0]['status'],'completed')
    def test_ambiguous_and_explicit_selection(self):
        self.proposal('unknown','refund')
        result=self.chat('我要退款');self.assertIsNone(result['commerce_case']);self.assertEqual(len(result['candidates']),8)
        obj=next(o for o in result['candidates'] if o['title']=='无线耳机')
        result=self.chat('选择无线耳机',conv_id=result['conversation_id'],order_id=obj['id'])
        self.assertEqual(result['commerce_case']['operations'][0]['quote']['amount_minor'],25900)
    def test_policy_no_case_and_sources(self):
        self.outputs['proposal']=json.dumps({'intents':[],'policy_only':True})
        self.outputs['policy_answer']='商品签收7天内需退货验收；模拟退款。'
        self.outputs['sources']=json.dumps([{'title':'商品退款','source':'Dify knowledge retrieval'}])
        r=self.chat('商品退款政策是什么？');self.assertIsNone(r['commerce_case']);self.assertTrue(r['sources'])
        self.assertIn('7天',r['response'])
    def test_close_renewal_preserves_entitlement(self):
        self.proposal('subscription','cancel_renewal','Basic')
        r=self.chat('Basic会员下个月别续了');c=r['commerce_case'];a=c['operations'][0]
        url='/commerce/objects/'+a['quote']['object_id'];old=self.client.get(url,headers=self.user).json()
        self.client.post(f"/commerce/cases/{c['id']}/decision",headers=self.user,json={'action_id':a['id'],'decision':'confirm'})
        new=self.client.get(url,headers=self.user).json();self.assertFalse(new['auto_renew']);self.assertEqual(old['entitlement'],new['entitlement']);self.assertEqual(new['refunds'],[])
    def test_model_cannot_pick_unspecified_object_or_grant_consent(self):
        self.proposal('goods','refund','无线耳机')
        r=self.chat('我要退款');self.assertIsNone(r['commerce_case']);self.assertEqual(len(r['candidates']),8)
        self.proposal('goods','refund','无线耳机')
        r=self.chat('帮我退无线耳机');c=r['commerce_case']
        r=self.chat('确认',conv_id=r['conversation_id']);self.assertEqual(r['mode'],'dify_workflow')
        cs=self.client.get('/commerce/cases',headers=self.user).json()['cases']
        self.assertEqual(cs[0]['operations'][0]['status'],'awaiting_confirmation')
    def test_invalid_output_and_foreign_objects_fail_closed(self):
        self.outputs['proposal']=json.dumps({'intents':[{'domain':'goods','operation':'approve'}]})
        r=self.chat('审批退款');self.assertIn('dify_workflow_failed',r['degradations'])
        self.assertEqual(self.client.get('/commerce/cases',headers=self.user).json()['cases'],[])
        other=self.client.post('/demo/session').json()
        obj=self.client.get('/commerce/objects',headers={'Authorization':'Bearer '+other['userToken']}).json()['objects'][0]
        self.proposal('goods','refund',obj['id']);r=self.chat('退掉'+obj['id']);self.assertIsNone(r['commerce_case'])
        sent=self.requests[-1]['inputs']['catalog'];self.assertNotIn(obj['id'],sent)

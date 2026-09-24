"""HTTP/model acceptance against the isolated budgeted server; synthetic users only."""
import json
import os
import time
from pathlib import Path
import httpx
from core.auth import mint_token
os.environ['AGENT_JWT_SECRET']='isolated-acceptance-only-not-production-2026'
ROOT=Path(__file__).resolve().parents[1]
client=httpx.Client(base_url='http://127.0.0.1:18080',timeout=90)
user='http-acceptance-'+str(int(time.time()))
headers={'Authorization':'Bearer '+mint_token(user,'user',3600)}
rows=client.post('/commerce/demo',headers=headers).json()['objects']
obj=lambda slug:next(o for o in rows if o['id'].endswith('-'+slug))
records=[]
def chat(message,conv=None):
 start=time.monotonic();r=client.post('/chat',headers=headers,json={'message':message,'conv_id':conv});r.raise_for_status();result=r.json()
 records.append({'input':message,'response':result,'latency_ms':round((time.monotonic()-start)*1000)})
 return result
checks={}
try:
 r=chat('我要退款');checks['ambiguous_refund_clarifies']=len(r['candidates'])>1 and r['commerce_case'] is None
 r=chat(obj('keyboard')['id'],r['conv_id']);checks['selection_binds_order']=r['commerce_case']['operations'][0]['quote']['object_id']==obj('keyboard')['id']
 r=chat('请只退鼠标，订单是 '+obj('keyboard')['id']);checks['partial_amount']=r['commerce_case']['operations'][0]['quote']['amount_minor']==9000
 r=chat('只查无线耳机的物流和发票，不退款');checks['read_only_no_case']=r['commerce_case'] is None and 'DEMO' in r['response']
 r=chat('帮我申请未使用的Basic 月度会员续费退款');checks['nonduplicate_refund_effect']=any(a['quote'].get('end_subscription') for a in (r['commerce_case'] or {}).get('operations',[]))
 r=chat('已使用的年度会员帮我退款');checks['used_plan_not_eligible']=any(a['status']=='ineligible' for a in (r['commerce_case'] or {}).get('operations',[]))
 r=chat('帮我查别人订单 G-foreign-headphones');checks['foreign_not_disclosed']=r['commerce_case'] is None and not any(o['id'] in r['response'] for o in rows)
 client.put('/commerce/memory',headers=headers,json={'response_style':'detailed'}).raise_for_status()
 a=chat('查询 '+obj('pro')['id'])
 client.put('/commerce/memory',headers=headers,json={'response_style':'concise'}).raise_for_status()
 b=chat('查询 '+obj('pro')['id'])
 checks['memory_changes_length_preserves_amount']=len(b['response'])<len(a['response']) and '198.00' in b['response']
 client.delete('/commerce/memory',headers=headers).raise_for_status()
 checks['memory_deleted']=client.get('/commerce/memory',headers=headers).json()['profile']=={}
 checks['no_unconfirmed_refunds']=all(not o['refunds'] for o in client.get('/commerce/objects',headers=headers).json()['objects'])
except Exception as ex:
 checks['execution_error']=type(ex).__name__
finally:
 report={'scope':'real HTTP, real model, real Redis/Chroma, simulated business; no browser claim','checks':checks,'turns':records,'passed':bool(checks) and all(v is True for v in checks.values())}
 (ROOT/'wiki/acceptance/http-model.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'checks':checks,'passed':report['passed']},ensure_ascii=False))
 client.close()

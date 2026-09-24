"""Controlled memory ablation. Real data stores, deterministic language adapter."""
import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path
from agents.action_runtime import ActionRuntime
from business.commerce import CommerceStore
from business.conversation import CommerceConversation
from memory.local_conversation_memory import LocalConversationMemory
from memory.conversation_memory import MemoryManager, MemoryContext

class NoMemory:
    mode='disabled'
    async def get_context(self,*args):return MemoryContext([],[],{},'')
    async def add_message(self,*args):pass
    async def update_profile(self,*args):pass
class NoModel:
    async def create(self,**kwargs):raise RuntimeError('deterministic ablation, no paid model')

async def run():
 results=[]
 with tempfile.TemporaryDirectory() as tmp:
  for backend in ('sqlite','redis_chroma'):
   runtime=ActionRuntime(tmp+'/'+backend+'.db');store=CommerceStore(runtime.path);owner='ablation-'+str(uuid.uuid4());rows=store.seed(owner);oid=next(o['id'] for o in rows if o['id'].endswith('pro'))
   mem=LocalConversationMemory(runtime.path) if backend=='sqlite' else MemoryManager(redis_url='redis://127.0.0.1:16379/0',chroma_host='127.0.0.1',chroma_port=18001,api_key='test')
   if backend=='redis_chroma':mem._client=NoModel()
   service=CommerceConversation(runtime,mem);off=CommerceConversation(runtime,NoMemory())
   await mem.set_profile(owner,{'response_style':'concise'})
   start=time.monotonic();a=await service.send(owner,'enabled','查询 '+oid);a_ms=(time.monotonic()-start)*1000
   start=time.monotonic();b=await off.send(owner,'disabled','查询 '+oid);b_ms=(time.monotonic()-start)*1000
   style={'enabled_chars':len(a['response']),'disabled_chars':len(b['response']),'enabled_ms':round(a_ms,2),'disabled_ms':round(b_ms,2),'same_current_amount':'198.00' in a['response'] and '198.00' in b['response']}
   # Genuine conversation writes; full adapter compression uses its documented
   # source-text fallback here, not a fabricated model-generated summary.
   for i in range(8):await service.send(owner,'prior','查询 '+oid)
   if backend=='redis_chroma':
    pending=list(mem._background_tasks)
    if pending:await asyncio.gather(*pending)
   with store.connect() as db:db.execute('DELETE FROM commerce_dialogs WHERE owner=?',(owner,))
   with_memory=await service.send(owner,'cross-session','上次那个订单查一下')
   without_memory=await off.send(owner,'no-context','上次那个订单查一下')
   continuity={'enabled_clarifications':int('请确认' in with_memory['response']),'disabled_clarifications':int('请确认' in without_memory['response']),'enabled_response':with_memory['response'],'disabled_response':without_memory['response']}
   await mem.forget(owner)
   deleted=await mem.get_context(owner,'prior','订单')
   results.append({'backend':backend,'style':style,'continuity':continuity,'deletion_clears_all':not deleted.user_profile and not deleted.relevant_history and not deleted.recent_messages})
   await mem.close()
 report={'scope':'controlled deterministic language ablation; real SQLite and Redis/Chroma; timings are single observations, not benchmarks','results':results}
 path=Path(__file__).resolve().parents[1]/'wiki/acceptance/memory-ablation.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
asyncio.run(run())

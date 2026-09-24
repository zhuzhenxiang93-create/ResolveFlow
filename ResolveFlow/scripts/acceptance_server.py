"""Isolated real API/Redis/Chroma server. --live has a persisted 60-call cap."""
import argparse
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1]

class BudgetClient:
    def __init__(self,client,path,limit=60):
        self.client,self.path,self.limit=client,Path(path),limit
        self.model=client.model
        self.records=json.loads(self.path.read_text()) if self.path.exists() else []
    async def call(self,method,**kwargs):
        if len(self.records)>=self.limit:raise RuntimeError('Acceptance budget exhausted')
        record={'number':len(self.records)+1,'method':method,'status':'started'}
        self.records.append(record);self.path.write_text(json.dumps(self.records,indent=2))
        try:
            result=await getattr(self.client,method)(**kwargs);record['status']='success'
            if isinstance(result,dict):record['usage']=result.get('_usage')
            return result
        except Exception as ex:
            record.update(status='error',error_type=type(ex).__name__);raise
        finally:self.path.write_text(json.dumps(self.records,indent=2))
    async def create(self,**kwargs):return await self.call('create',**kwargs)
    async def create_tool_turn(self,**kwargs):return await self.call('create_tool_turn',**kwargs)

class OfflineSummary:
    model='offline-no-model'
    async def create(self,**kwargs):raise RuntimeError('Automatic profile/summary unavailable without model')

def configure(live=False,directory='/tmp/resolveflow-acceptance'):
    from business.execution import ExecutionContext
    from agents.conversation_service import ConversationService
    from agents.agent_orchestrator import AgentOrchestrator
    from core.intent_recognizer import IntentRecognizer
    from core.llm_client import LLMClient
    from memory.conversation_memory import MemoryManager
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager,Tool
    from api import main,action_routes,commerce_routes
    from core.auth import mint_token
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    os.environ['AGENT_JWT_SECRET']='isolated-acceptance-only-not-production-2026'
    client=None
    if live:
        cfg={**dotenv_values(ROOT/'.env'),**dotenv_values(ROOT/'.env.agent.local')}
        raw=LLMClient(api_key=cfg['LLM_API_KEY'],model=cfg['LLM_MODEL'],provider=cfg.get('LLM_PROVIDER','openai'),base_url=cfg.get('LLM_BASE_URL'),max_retries=0)
        client=BudgetClient(raw,directory/'model-calls.json')
    runtime=ExecutionContext(directory/'state.sqlite3',client=client)
    memory=MemoryManager(redis_url='redis://127.0.0.1:16379/0',chroma_host='127.0.0.1',chroma_port=18001,chroma_path=str(directory/'chroma'),api_key='offline-placeholder')
    memory._client=client or OfflineSummary()
    recognizer=IntentRecognizer(api_key='',client=client,use_llm=bool(client),base_url='local-compatible')
    kb=KnowledgeBase(chroma_host='127.0.0.1',chroma_port=18001,chroma_path=str(directory/'chroma'),load_default_docs=False)
    kb.add_documents(json.loads((ROOT/'data/knowledge/commerce_policy_v1.json').read_text()))
    manager=MCPToolManager(api_key='offline-placeholder');manager._client=client or OfflineSummary()
    manager.register(Tool(name='knowledge_search',description='Knowledge',handler=kb.search_handler,schema={'type':'object','properties':{'query':{'type':'string'},'top_k':{'type':'integer'},'allowed_document_ids':{'type':'array'}},'required':['query']}))
    orchestrator=AgentOrchestrator(api_key='',client=client,recognizer=recognizer,tool_manager=manager) if client else None
    service=ConversationService(runtime,memory=memory,recognizer=recognizer,answer_orchestrator=orchestrator)
    main._memory=memory;main._tool_manager=manager;main._orchestrator=orchestrator
    action_routes.runtime=lambda:runtime;action_routes.conversation_service=lambda:service
    commerce_routes.runtime=lambda:runtime;commerce_routes.conversation_service=lambda:service
    async def search(query,allowed_document_ids=None):
        if not client:return await kb.search_async(query,5,allowed_document_ids=allowed_document_ids)
        r=await manager.search_with_rewrite('knowledge_search',query,top_k=5,extra_params={'allowed_document_ids':sorted(allowed_document_ids)} if allowed_document_ids is not None else None)
        return r.data if r.success else []
    service.knowledge_search=search;service.knowledge_document_ids=kb.document_ids
    main._configure_conversation_service=lambda:service
    tokens={role:mint_token('acceptance-user' if role=='user' else 'acceptance-'+role,role,86400) for role in ('user','reviewer','admin')}
    (directory/'tokens.json').write_text(json.dumps(tokens));os.chmod(directory/'tokens.json',0o600)
    @asynccontextmanager
    async def lifespan(app):
        yield
        await memory.close()
        if client:await client.client._client.close()
    main.app.router.lifespan_context=lifespan
    return main.app

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--port',type=int,default=18080);parser.add_argument('--directory',default='/tmp/resolveflow-acceptance');args=parser.parse_args()
    uvicorn.run(configure(args.live,args.directory),host='127.0.0.1',port=args.port)

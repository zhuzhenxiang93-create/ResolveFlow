"""Explicit opt-in, isolated demo host. Never mounted by the production app."""
import os
import secrets
import uuid
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from api import commerce_routes, eval_routes
from api.action_routes import owner
from agents.conversation_service import ConversationService
from business.execution import ExecutionContext
from core.auth import AuthError, decode_token, mint_token


class PortfolioKnowledgeTools:
    """Read-only local retrieval; explicitly lexical, never advertised as hybrid RAG."""
    knowledge_description = '检索演示客服政策（本地 BM25 词法检索，无向量召回或模型重排），返回可核查来源。'

    def __init__(self, kb):
        self.kb = kb

    async def search_with_rewrite(self, name, query, top_k=5, extra_params=None):
        from mcp.tool_manager import ToolResult
        if name != 'knowledge_search':
            return ToolResult(False, [], name, error='Unknown read-only tool')
        items = await self.kb.search_async(query, top_k=top_k,
            allowed_document_ids=(extra_params or {}).get('allowed_document_ids'))
        return ToolResult(True, items, name, degradations=['lexical_retrieval'])


def create_app():
    if os.getenv('RESOLVEFLOW_DEMO_MODE') != 'true':
        raise RuntimeError('Set RESOLVEFLOW_DEMO_MODE=true for the isolated demo host')
    root = Path(os.getenv('RESOLVEFLOW_DEMO_DIR', '/tmp/resolveflow-portfolio')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = root / 'signing-key'
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    os.environ['AGENT_JWT_SECRET'] = key.read_text()
    current = ContextVar('portfolio_session')
    services = {}
    client = None
    if os.getenv('AGENT_USE_LLM') == '1':
        from core.llm_client import LLMClient
        client = LLMClient(api_key=os.environ['LLM_API_KEY'], model=os.environ['LLM_MODEL'],
                           provider=os.getenv('LLM_PROVIDER', 'openai'), base_url=os.getenv('LLM_BASE_URL'))

    def service(session):
        if session not in services:
            svc = ConversationService(ExecutionContext(root / f'{session}.sqlite3', client=client))
            if client:
                from agents.agent_orchestrator import AgentOrchestrator
                manager = PortfolioKnowledgeTools(svc.kb)
                svc.answer_orchestrator = AgentOrchestrator(api_key='', client=client,
                    model=client.model, recognizer=svc.recognizer, tool_manager=manager)
                async def search(query, allowed_document_ids=None):
                    result = await manager.search_with_rewrite('knowledge_search', query, top_k=10,
                        extra_params={'allowed_document_ids': allowed_document_ids})
                    return result.data
                svc.knowledge_search = search
                from datetime import date
                svc.knowledge_document_ids = lambda domain=None: {
                    d['id'] for d in svc.documents if (domain is None or d.get('domain') == domain)
                    and d.get('effective_at', '2000-01-01') <= date.today().isoformat()}
            services[session] = svc
        return services[session]

    # This module is a standalone process: production api.main is never imported.
    commerce_routes.runtime = lambda: service(current.get()).runtime
    commerce_routes.conversation_service = lambda: service(current.get())
    app = FastAPI(title='ResolveFlow isolated portfolio demo')

    @app.middleware('http')
    async def session_scope(request: Request, call_next):
        if request.url.path in {'/health', '/demo/session', '/docs', '/openapi.json'} or request.url.path.startswith('/eval/'):
            return await call_next(request)
        from starlette.responses import JSONResponse
        try:
            auth = request.headers.get('authorization', '')
            if not auth.startswith('Bearer '):
                raise AuthError('Missing bearer token')
            claims = decode_token(auth[7:])
            session, role = str(claims['sub']).split(':')
            if str(uuid.UUID(session)) != session or role != claims['role'] or role not in {'user', 'reviewer'}:
                raise ValueError('Invalid demo identity')
            if not (root / f'{session}.sqlite3').is_file():
                raise ValueError('Demo session expired')
        except (AuthError, ValueError):
            return JSONResponse({'detail': 'Demo session unavailable. Reset Demo to begin again.'}, status_code=401)
        token = current.set(session)
        try:
            return await call_next(request)
        finally:
            current.reset(token)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'demo': True, 'mode': 'live-model' if client else 'offline-rules',
                'retrieval': 'lexical', 'simulated': True}

    @app.post('/demo/session')
    def bootstrap():
        # A fresh namespace is also the reset operation: existing audits are retained.
        session = str(uuid.uuid4())
        user = f'{session}:user'
        service(session).execution.store.seed(user)
        return {'userId': user, 'userToken': mint_token(user, 'user', 86400),
                'reviewerToken': mint_token(f'{session}:reviewer', 'reviewer', 86400),
                'mode': 'live-model' if client else 'offline-rules', 'simulated': True}

    class Chat(BaseModel):
        model_config = ConfigDict(extra='forbid')
        message: str = Field(min_length=1, max_length=4000)
        user_id: str = ''
        conv_id: str | None = None
        order_id: str | None = None

    @app.post('/chat')
    async def chat(body: Chat, user=Depends(owner)):
        try:
            result = await service(current.get()).send(user, body.message,
                        conversation_id=body.conv_id, order_id=body.order_id)
            if not client and any(word in body.message for word in ('401', '登录', '报错')):
                if not result.get('support_result'):
                    result['response'] += '\n离线模式未连接 Technical Agent；技术问题需要启用模型后处理。'
                result.setdefault('degradations', []).append('technical_agent_offline')
            return result
        except ValueError as ex:
            raise HTTPException(400, str(ex))

    app.include_router(commerce_routes.router)
    # Static, read-only evaluation reports; public within the local Demo, never runs a model.
    app.include_router(eval_routes.router)
    return app

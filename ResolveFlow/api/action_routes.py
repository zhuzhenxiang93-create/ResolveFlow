"""Execution API; identity is a local JWT carrying a subject and one of three
roles (user / reviewer / admin) — see core/auth.py. Still single-node, still
not production IAM, but no longer hardcoded to exactly one user and one
reviewer identity."""
import hmac
import os
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from business.execution import ExecutionContext
from agents.agent_orchestrator import AgentOrchestrator
from core.auth import AuthError, ROLES, mint_token, subject_with_role

router = APIRouter(prefix="/agent", tags=["Agent execution (simulated)"])


def owner(authorization: str = Header(default="")):
    try:
        return subject_with_role(authorization, "user")
    except AuthError as ex:
        raise HTTPException(401, str(ex)) from ex


def reviewer(authorization: str = Header(default="")):
    try:
        return subject_with_role(authorization, "reviewer")
    except AuthError as ex:
        raise HTTPException(403, str(ex)) from ex


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=100)
    role: str
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


@router.post("/auth/token")
def issue_token(req: TokenRequest, x_admin_secret: str = Header(default="")):
    """Bootstrap endpoint: mints a role-scoped JWT for a subject. Gated by a
    separate, non-JWT root secret (AGENT_ADMIN_SECRET) — a JWT cannot be used
    to mint another JWT, there has to be one non-JWT trust anchor."""
    admin_secret = os.getenv("AGENT_ADMIN_SECRET", "")
    if not admin_secret or not hmac.compare_digest(x_admin_secret or "", admin_secret):
        raise HTTPException(403, "Configure AGENT_ADMIN_SECRET and supply X-Admin-Secret")
    if req.role not in ROLES:
        raise HTTPException(400, "role must be one of: " + ", ".join(sorted(ROLES)))
    try:
        token = mint_token(req.subject, req.role, req.ttl_seconds)
    except AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    return {"access_token": token, "token_type": "bearer", "subject": req.subject,
            "role": req.role, "expires_at": time.time() + req.ttl_seconds}


@lru_cache(maxsize=1)
def runtime():
    client = None
    if os.getenv("AGENT_USE_LLM", "0") == "1":
        from core.llm_client import LLMClient
        client = LLMClient(api_key=os.environ["LLM_API_KEY"],
                           base_url=os.getenv("LLM_BASE_URL"),
                           model=os.environ["LLM_MODEL"],
                           # 和 _llm_cfg()（api/main.py）共用同一组 LLM_* 变量，
                           # 这里显式传 provider，不依赖 LLMClient 内部对
                           # LLM_PROVIDER 的隐式 fallback。
                           provider=os.getenv("LLM_PROVIDER", "openai"))
    path = os.getenv("AGENT_STATE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "agent" / "state.sqlite3"))
    return ExecutionContext(path, client=client)


@lru_cache(maxsize=8)
def conversation_service_for(executor):
    from agents.conversation_service import ConversationService
    return ConversationService(executor)


def conversation_service():
    return conversation_service_for(runtime())


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRequest(StrictRequest):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = None


class ContinueRequest(StrictRequest):
    order_id: Optional[str] = None
    message: str = Field(default="", max_length=4000)
    conversation_id: Optional[str] = None


class ApprovalRequest(StrictRequest):
    approval_id: str
    approved: StrictBool


class ConfirmationRequest(StrictRequest):
    confirmation_id: str
    accepted: StrictBool


class ConversationRequest(StrictRequest):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = None
    task_id: Optional[str] = None
    order_id: Optional[str] = None


@router.post("/conversation")
async def conversation(req: ConversationRequest, user=Depends(owner)):
    try:
        return await conversation_service().send(user, req.message, conversation_id=req.conversation_id,
                                                 task_id=req.task_id, order_id=req.order_id)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


def retired():
    raise HTTPException(410, {"code": "legacy_execution_retired",
        "message": "旧执行接口已停用。请通过 /chat 重新申请，并使用返回的 commerce_case 中具体操作；旧确认和审批不可复用。",
        "chat_endpoint": "/chat", "decision_endpoint": "/commerce/cases/{case_id}/decision"})


@router.get("/review/tasks")
def review_queue(actor=Depends(reviewer)):
    from business.commerce import CommerceStore
    return {"tasks": CommerceStore(runtime().path).review_queue(), "execution_engine": "commerce"}


@router.get("/review/tasks/{task_id}")
def review_detail(task_id: str, actor=Depends(reviewer)):
    from business.commerce import CommerceStore
    store=CommerceStore(runtime().path)
    with store.connect() as db:
        table = "commerce_cases" if task_id.startswith("C-") else "tasks"
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
            raise HTTPException(404,"Task not found")
        row=db.execute("SELECT owner FROM " + table + " WHERE id=?",(task_id,)).fetchone()
    if not row:raise HTTPException(404,"Task not found")
    return {"task": ExecutionContext(runtime().path).get(task_id,row[0]),
            "note": "查询结果不代表审批；执行统一使用 /commerce 接口。"}


@router.post("/demo/orders")
def seed(user=Depends(owner)):
    from business.commerce import CommerceStore
    return {"objects": CommerceStore(runtime().path).seed(user), "simulated": True}


@router.post("/tasks")
async def start(req: StartRequest, user=Depends(owner)):
    return await conversation_service().send(user, req.message, conversation_id=req.conversation_id)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(owner)):
    try:
        return ExecutionContext(runtime().path).get(task_id, user)
    except ValueError as ex:
        raise HTTPException(404, str(ex)) from ex


@router.post("/tasks/{task_id}/confirmation")
async def confirm(task_id: str, req: ConfirmationRequest, user=Depends(owner)):
    retired()


@router.post("/tasks/{task_id}/continue")
async def resume(task_id: str, req: ContinueRequest, user=Depends(owner)):
    retired()


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, user=Depends(owner)):
    retired()


@router.post("/tasks/{task_id}/revise")
async def revise(task_id: str, req: StartRequest, user=Depends(owner)):
    retired()


@router.post("/tasks/{task_id}/approval")
async def approve(task_id: str, req: ApprovalRequest, actor=Depends(reviewer)):
    retired()


@router.post("/tasks/{task_id}/release")
async def release_handoff(task_id: str, actor=Depends(reviewer)):
    retired()

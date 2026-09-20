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

from agents.action_runtime import ActionRuntime
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
                           model=os.environ["LLM_MODEL"])
    path = os.getenv("AGENT_STATE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "agent" / "state.sqlite3"))
    return ActionRuntime(path, client=client, allow_fallback=False)


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


@router.get("/review/tasks")
def review_queue(actor=Depends(reviewer)):
    # A reviewer is a separate role, not scoped to one owner — the queue spans
    # every user's tasks that are pending approval or stuck on a human.
    with runtime().connect() as db:
        rows = db.execute("SELECT body FROM tasks WHERE json_extract(body, '$.status') IN (?, ?) ORDER BY rowid DESC LIMIT 100",
                          ("awaiting_approval", "needs_human")).fetchall()
    return {"tasks": [{k: t.get(k) for k in ("id", "owner", "status", "order_id", "response", "approval")}
                      for t in (json.loads(row[0]) for row in rows)], "limit": 100}


@router.get("/review/tasks/{task_id}")
def review_detail(task_id: str, actor=Depends(reviewer)):
    try:
        rt = runtime()
        owner_id = rt.owner_of(task_id)
        with rt.connect() as db:
            task = rt._load(db, task_id, owner_id)
            order = rt._order(db, task)
        approval = task.get("approval")
        return {"task": task, "current_order": order, "read_at": time.time(),
                "approval_expired": bool(approval and approval["expires_at"] <= time.time()),
                "note": "Evidence is historical. Approval endpoint revalidates consent, versions, expiry and business preconditions."}
    except ValueError as ex:
        raise HTTPException(404, str(ex)) from ex


@router.post("/tasks/{task_id}/confirmation")
async def confirm(task_id: str, req: ConfirmationRequest, user=Depends(owner)):
    try:
        task = runtime().confirm(task_id, user, req.confirmation_id, req.accepted)
        if task["status"] == "running":
            task = await AgentOrchestrator.execute_action(runtime(), owner=user, task_id=task_id)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/demo/orders")
def seed(user=Depends(owner)):
    return runtime().seed(user)


@router.post("/tasks")
async def start(req: StartRequest, user=Depends(owner)):
    task = await AgentOrchestrator.execute_action(runtime(), owner=user, message=req.message, conversation_id=req.conversation_id)
    await conversation_service().observe(task)
    return task


@router.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(owner)):
    try:
        return runtime().get(task_id, user)
    except ValueError as ex:
        raise HTTPException(404, str(ex)) from ex


@router.post("/tasks/{task_id}/continue")
async def resume(task_id: str, req: ContinueRequest, user=Depends(owner)):
    try:
        task = await AgentOrchestrator.execute_action(runtime(), owner=user, task_id=task_id, order_id=req.order_id,
                                                     message=req.message, conversation_id=req.conversation_id)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, user=Depends(owner)):
    try:
        task = runtime().cancel(task_id, user)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/tasks/{task_id}/revise")
async def revise(task_id: str, req: StartRequest, user=Depends(owner)):
    try:
        task = runtime().revise(task_id, user, req.message)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/tasks/{task_id}/approval")
async def approve(task_id: str, req: ApprovalRequest, actor=Depends(reviewer)):
    try:
        owner_id = runtime().owner_of(task_id)
        task = runtime().approve(task_id, owner_id, req.approval_id, req.approved, actor)
        if task["status"] == "running":
            task = await AgentOrchestrator.execute_action(runtime(), owner=owner_id, task_id=task_id)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/tasks/{task_id}/release")
async def release_handoff(task_id: str, actor=Depends(reviewer)):
    try:
        owner_id = runtime().owner_of(task_id)
        runtime().release_handoff(task_id, owner_id, actor)
        task = await AgentOrchestrator.execute_action(runtime(), owner=owner_id, task_id=task_id)
        await conversation_service().observe(task)
        return task
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex

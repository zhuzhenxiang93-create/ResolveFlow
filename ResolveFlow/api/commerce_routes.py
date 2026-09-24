"""Commerce APIs share existing JWT roles and the unified conversation memory."""
from __future__ import annotations
from typing import Literal, Optional
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from api.action_routes import owner, reviewer, runtime, conversation_service
from business.commerce import CommerceStore

router = APIRouter(prefix="/commerce", tags=["Commerce (simulated)"])


def store():
    return CommerceStore(runtime().path)


@router.post("/demo")
def seed(user=Depends(owner)):
    return {"objects": store().seed(user), "simulated": True}


@router.get("/objects")
def objects(user=Depends(owner)):
    return {"objects": store().catalog(user)}


@router.get("/objects/{object_id}")
def detail(object_id: str, user=Depends(owner)):
    try:
        return store().detail(user, object_id)
    except ValueError as ex:
        raise HTTPException(404, str(ex))


@router.get("/cases")
def cases(conversation: Optional[str] = None, user=Depends(owner)):
    return {"cases": store().cases(user, conversation)}


class UserDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    decision: Literal["confirm", "cancel"]


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str
    decision: Literal["approve", "reject", "receive_return", "settle", "fail"]


@router.post("/cases/{case_id}/decision")
async def decide(case_id: str, req: UserDecision, user=Depends(owner)):
    try:
        case = store().transition(user, case_id, req.action_id, req.decision, user)
        from memory.conversation_memory import MsgRole
        try:
            await conversation_service().memory.add_message(user, case["conversation_id"], MsgRole.ASSISTANT, case["response"])
        except Exception:
            logging.getLogger(__name__).exception("Memory unavailable after committed commerce decision")
            case["degradations"] = ["memory_write_failed"]
        return case
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@router.get("/review")
def queue(actor=Depends(reviewer)):
    return {"cases": store().review_queue()}


@router.post("/review/{case_id}")
def review(case_id: str, req: ReviewDecision, actor=Depends(reviewer)):
    service = store()
    with service.connect() as db:
        row = db.execute("SELECT owner FROM commerce_cases WHERE id=?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    try:
        return service.transition(row[0], case_id, req.action_id, req.decision, actor)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@router.get("/memory")
async def memory(user=Depends(owner)):
    adapter = conversation_service().memory
    status = await adapter.profile_status(user) if hasattr(adapter, "profile_status") else {"state": "synchronous"}
    return {"profile": await adapter.get_profile(user), "mode": getattr(adapter, "mode", "redis_chroma"), "update_status": status}


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_style: Literal["concise", "detailed"]


@router.put("/memory")
async def update_memory(req: ProfileUpdate, user=Depends(owner)):
    adapter = conversation_service().memory
    await adapter.set_profile(user, req.model_dump())
    return {"profile": await adapter.get_profile(user)}


@router.delete("/memory")
async def delete_memory(user=Depends(owner)):
    await conversation_service().memory.forget(user)
    # Also remove remembered object references, never erase transactional audit.
    with store().connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='commerce_dialogs'").fetchone()
        if exists:
            db.execute("DELETE FROM commerce_dialogs WHERE owner=?", (user,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='commerce_consultations'").fetchone():
            db.execute("DELETE FROM commerce_consultations WHERE owner=?", (user,))
        db.execute("UPDATE unified_conversations SET last_order=NULL WHERE owner=?", (user,))
    return {"deleted": True, "note": "已删除对话记忆与偏好；业务记录和审计保留"}

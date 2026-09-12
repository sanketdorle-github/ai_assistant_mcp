"""User-facing control over long-term memory (agents/memory.py: mem0 +
Qdrant) - the "what does the agent remember about me" surface. Distinct
from `/chat/threads`, which is session/conversation history, not long-term
memory.
"""

from fastapi import APIRouter, Depends, HTTPException

from mcp_orchestration.agents import memory as long_term_memory
from mcp_orchestration.api.routes.auth import get_current_user

router = APIRouter(prefix="/memory", tags=["Long-term Memory"])


@router.get("")
def list_memories(current_user: dict = Depends(get_current_user)):
    """List everything long-term memory holds for the current user (facts
    and episode summaries alike)."""
    return long_term_memory.list_all(current_user["id"])


@router.delete("/{memory_id}")
def delete_memory(memory_id: str, current_user: dict = Depends(get_current_user)):
    """Delete one memory - only if it belongs to the current user. mem0's
    own delete() has no ownership check built in, so agents/memory.py's
    `forget()` verifies membership first."""
    deleted = long_term_memory.forget(current_user["id"], memory_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Memory not found (or long-term memory isn't configured).",
        )
    return {"deleted": True}


@router.delete("")
def clear_memories(current_user: dict = Depends(get_current_user)):
    """Delete every long-term memory for the current user - the "forget
    everything about me" action."""
    long_term_memory.forget_all(current_user["id"])
    return {"cleared": True}

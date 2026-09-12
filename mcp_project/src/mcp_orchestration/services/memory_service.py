import asyncio
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple
from bson.binary import Binary
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
)
from mcp_orchestration.core.database import database, DatabaseUnavailableError

logger = logging.getLogger("mcp_orchestration.services.memory_service")


def _checkpoints_collection():
    """Resolve the collection only when a checkpoint operation is requested."""
    return database.collection("checkpoints")


class MongoCheckpointSaver(BaseCheckpointSaver):
    """Custom LangGraph Checkpoint Saver that persists agent execution state in MongoDB."""

    def _serialize(self, obj: Any) -> Dict[str, Any]:
        """Serialize an object via the checkpointer's serde.

        Newer langgraph-checkpoint versions (this project pins >=4.x) dropped
        the old `serde.dumps(obj) -> bytes` / `serde.loads(bytes) -> obj`
        methods in favour of `dumps_typed(obj) -> (type, bytes)` /
        `loads_typed((type, bytes)) -> obj`, so the encoding type has to be
        stored alongside the bytes to deserialize correctly later.
        """
        type_, data = self.serde.dumps_typed(obj)
        return {"type": type_, "data": Binary(data)}

    def _deserialize(self, stored: Dict[str, Any]) -> Any:
        """Inverse of `_serialize`."""
        return self.serde.loads_typed((stored["type"], bytes(stored["data"])))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> Dict[str, Any]:
        """Save a checkpoint snapshot to MongoDB."""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = checkpoint["id"]

        # Serialize the checkpoint and metadata using the default LangGraph serializer
        serialized_checkpoint = self._serialize(checkpoint)
        serialized_metadata = self._serialize(metadata)

        # Best-effort: pull user_id out of the graph state so threads can be
        # listed/filtered per-owner without deserializing every checkpoint.
        channel_values = checkpoint.get("channel_values") or {}
        user_id = channel_values.get("user_id")

        doc = {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
            "user_id": user_id,
            "checkpoint": serialized_checkpoint,
            "metadata": serialized_metadata,
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
        }

        # Upsert checkpoint
        _checkpoints_collection().replace_one(
            {"thread_id": thread_id, "checkpoint_id": checkpoint_id}, doc, upsert=True
        )

        logger.info(
            f"Saved LangGraph checkpoint '{checkpoint_id}' for thread '{thread_id}' to MongoDB."
        )

        return {
            "configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}
        }

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Retrieve a specific checkpoint tuple from MongoDB by thread_id and optional checkpoint_id."""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id")

        query = {"thread_id": thread_id}
        if checkpoint_id:
            query["checkpoint_id"] = checkpoint_id
        else:
            # If no checkpoint_id specified, grab the latest one
            # We cannot easily sort binary checkpoints in Mongo unless we sort by parent relationship or natural order
            pass

        if not checkpoint_id:
            # Newest checkpoint for this thread, by insertion order (_id is
            # monotonically increasing for a single-writer MongoClient ObjectId).
            doc = _checkpoints_collection().find_one(query, sort=[("_id", -1)])
            if not doc:
                return None
        else:
            doc = _checkpoints_collection().find_one(query)
            if not doc:
                return None

        # Deserialize checkpoint and metadata
        checkpoint = self._deserialize(doc["checkpoint"])
        metadata = self._deserialize(doc["metadata"])

        parent_config = None
        if doc.get("parent_checkpoint_id"):
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": doc["parent_checkpoint_id"],
                }
            }

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": doc["checkpoint_id"],
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
        )

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints matching the filter criteria."""
        query = {}
        if config:
            query["thread_id"] = config["configurable"]["thread_id"]

        if before:
            # list checkpoints prior to this parent id
            query["checkpoint_id"] = before["configurable"]["checkpoint_id"]

        # Add optional custom mongo filtering
        if filter:
            query.update(filter)

        cursor = _checkpoints_collection().find(query)
        if limit:
            cursor = cursor.limit(limit)

        for doc in cursor:
            checkpoint = self._deserialize(doc["checkpoint"])
            metadata = self._deserialize(doc["metadata"])

            parent_config = None
            if doc.get("parent_checkpoint_id"):
                parent_config = {
                    "configurable": {
                        "thread_id": doc["thread_id"],
                        "checkpoint_id": doc["parent_checkpoint_id"],
                    }
                }

            yield CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": doc["thread_id"],
                        "checkpoint_id": doc["checkpoint_id"],
                    }
                },
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=parent_config,
            )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: List[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes for a checkpoint (e.g. tool calls staged before
        an interrupt fires). Required by BaseCheckpointSaver; without this,
        graphs compiled with `interrupt_before` can lose in-flight state.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"]["checkpoint_id"]

        serialized_writes = [
            {"channel": channel, "value": self._serialize(value)}
            for channel, value in writes
        ]

        _writes_collection().update_one(
            {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
                "task_id": task_id,
            },
            {"$set": {"task_path": task_path, "writes": serialized_writes}},
            upsert=True,
        )

    # --- Async wrappers -------------------------------------------------
    # pymongo is synchronous; LangGraph's async graph execution (astream_events,
    # aget_state, aupdate_state) calls the a* methods below, so we bridge to
    # the sync implementations via a thread pool rather than blocking the
    # event loop directly.

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: List[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
        filter: Optional[Dict[str, Any]] = None,
    ):
        for item in await asyncio.to_thread(
            lambda: list(self.list(config, before=before, limit=limit, filter=filter))
        ):
            yield item


def _writes_collection():
    """Resolve the pending-writes collection only when needed."""
    return database.collection("checkpoint_writes")


def list_threads_for_user(user_id: str, limit: int = 50) -> list[dict]:
    """Return this user's chat threads, most recently active first.

    Best-effort: an empty list (rather than an exception) is returned if
    the database is unreachable, since this backs a read-only listing UI.
    `last_activity` is derived from the latest checkpoint's ObjectId
    (monotonically increasing for a single-writer MongoClient) - used by
    the episodic-memory idle-thread check in agents/memory.py, not just for
    display.
    """
    try:
        collection = _checkpoints_collection()
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$sort": {"_id": -1}},
            {"$group": {"_id": "$thread_id", "updated_at": {"$first": "$_id"}}},
            {"$sort": {"updated_at": -1}},
            {"$limit": limit},
        ]
        return [
            {
                "thread_id": doc["_id"],
                "last_activity": doc["updated_at"].generation_time,
            }
            for doc in collection.aggregate(pipeline)
        ]
    except DatabaseUnavailableError:
        logger.warning("Could not list threads, database unavailable.")
        return []


def get_thread_messages(thread_id: str) -> list[dict]:
    """Reconstruct the chat transcript for a thread from its latest checkpoint."""
    saver = MongoCheckpointSaver()
    try:
        tup = saver.get_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_id": None}}
        )
    except DatabaseUnavailableError:
        return []
    if tup is None:
        return []

    messages = (tup.checkpoint.get("channel_values") or {}).get("messages", [])

    transcript = []
    for m in messages:
        role = getattr(m, "type", "unknown")

        # Raw tool-call output ("tool" messages) and the planner's internal
        # rationale/step-list scaffold (AIMessages tagged "internal" in
        # agent.py's planner_node) are execution-trace detail, not
        # conversational turns - they're kept in the graph's own message
        # history for the planner/resolver/responder LLM calls, but
        # shouldn't render as if the assistant said them. The structured
        # plan is already available to the frontend via the "plan" SSE
        # event (see chat.py).
        if role == "tool":
            continue
        if role == "ai" and getattr(m, "additional_kwargs", {}).get("internal"):
            continue

        transcript.append({"role": role, "content": getattr(m, "content", "")})

    return transcript

# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""
Persistence checkpointer implementation using BaseKVStore interface.

This module provides a checkpointer implementation that uses BaseKVStore
for persistent storage, supporting any KV store implementation (shelve, database, etc.).
"""

import base64
from abc import ABC
from pathlib import Path
from typing import (
    Any,
    Optional,
    Tuple,
)

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

from openjiuwen.core.common.constants.constant import INTERACTIVE_INPUT
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import session_logger, LogEventType
from openjiuwen.core.foundation.store import (
    BaseKVStore,
    DbBasedKVStore,
)
from openjiuwen.core.foundation.store.kv import ShelveStore
from openjiuwen.core.graph.pregel import TASK_STATUS_INTERRUPT
from openjiuwen.core.graph.store import (
    create_serializer,
    GraphState,
    Serializer,
    Store,
)
from openjiuwen.core.session import (
    BaseSession,
    Checkpointer,
    InteractiveInput,
    NodeSession,
)
from openjiuwen.core.session.checkpointer import (
    build_key,
    build_key_with_namespace,
    CheckpointerFactory,
    CheckpointerProvider,
    SESSION_NAMESPACE_AGENT,
    SESSION_NAMESPACE_WORKFLOW,
    Storage,
    WORKFLOW_NAMESPACE_GRAPH,
)
from openjiuwen.core.session.constants import FORCE_DEL_WORKFLOW_STATE_KEY


class BaseStorage(Storage, ABC):
    """
    Base class for persistence-based storage implementations with common functionality.
    
    This class uses BaseKVStore interface and does not depend on specific implementations.
    """

    def __init__(self, kv_store: BaseKVStore):
        """
        Initialize BaseStorage with a BaseKVStore instance.

        Args:
            kv_store (BaseKVStore): The BaseKVStore instance for all storage operations.
            ttl (Optional[dict[str, Any]]): Optional TTL configuration.
        """
        self._kv_store = kv_store
        self._serde: Serializer = create_serializer("pickle")

    def _serialize_state(self, state: Any) -> Optional[Tuple[str, bytes]]:
        """Serialize state and return (dump_type, blob) tuple."""
        return self._serde.dumps_typed(state)

    def _decode_dump_type(self, dump_type: Any) -> str:
        """Decode dump_type from bytes to string if needed."""
        if isinstance(dump_type, bytes):
            return dump_type.decode("utf-8")
        return dump_type if dump_type is not None else ""

    def _deserialize_state(self, dump_type: Any, blob: Any) -> Any:
        """Deserialize state from (dump_type, blob) tuple."""
        if dump_type is None or blob is None:
            return None
        # Decode dump_type if needed
        dump_type_str = self._decode_dump_type(dump_type)
        try:
            # Convert blob string back to bytes if needed
            if isinstance(blob, str):
                # Assume base64 encoding for bytes stored as string
                blob_bytes = base64.b64decode(blob)
            else:
                blob_bytes = blob
            return self._serde.loads_typed((dump_type_str, blob_bytes))
        except Exception as e:
            session_logger.error(
                "Failed to deserialize state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                error_message=str(e),
                metadata={"operation": "deserialize"}
            )
            return None


class AgentStorage(BaseStorage):
    """Agent state storage using BaseKVStore."""

    _STATE_BLOBS = "agent_state_blobs"
    _STATE_BLOBS_DUMP_TYPE = "agent_state_blobs_dump_type"
    _KEY_NUMS = 2

    async def save(self, session: BaseSession):
        """Save agent state to KV store."""
        state = session.state().get_state()
        session_id = session.session_id()
        agent_id = session.agent_id()

        state_blob = self._serialize_state(state)
        if not state_blob:
            session_logger.warning(
                "Failed to serialize agent state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"operation": "serialize"}
            )
            return

        try:
            dump_type, blob = state_blob
            pipeline = self._kv_store.pipeline()
            dump_type_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS_DUMP_TYPE
            )
            blob_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS
            )
            await pipeline.set(dump_type_key, dump_type)
            await pipeline.set(blob_key, blob)
            await pipeline.execute()
            session_logger.debug(
                "Agent state saved successfully",
                event_type=LogEventType.CHECKPOINT_SAVE,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"storage_type": "persistence"}
            )
        except Exception as e:
            session_logger.error(
                "Failed to save agent state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                agent_id=agent_id,
                error_message=str(e),
                metadata={"operation": "save", "storage_type": "persistence"}
            )
            raise

    async def recover(self, session: BaseSession, inputs: InteractiveInput = None):
        """Recover agent state from KV store."""
        session_id = session.session_id()
        agent_id = session.agent_id()

        pipeline = self._kv_store.pipeline()
        dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS_DUMP_TYPE
        )
        blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS
        )
        await pipeline.get(dump_type_key)
        await pipeline.get(blob_key)
        results = await pipeline.execute()

        if len(results) != self._KEY_NUMS:
            session_logger.debug(
                "Unexpected key count during agent state recovery",
                event_type=LogEventType.CHECKPOINT_RESTORE,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"expected_keys": self._KEY_NUMS, "actual_keys": len(results)}
            )
            return

        dump_type, blob = results[0], results[1]
        state = self._deserialize_state(dump_type, blob)
        if state is None:
            session_logger.debug(
                "No agent state found",
                event_type=LogEventType.CHECKPOINT_RESTORE,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"storage_type": "persistence"}
            )
            return

        try:
            session.state().set_state(state)
            session_logger.debug(
                "Agent state recovered successfully",
                event_type=LogEventType.CHECKPOINT_RESTORE,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"storage_type": "persistence"}
            )
        except Exception as e:
            session_logger.error(
                "Failed to set agent state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                agent_id=agent_id,
                error_message=str(e),
                metadata={"operation": "set_state"}
            )
            raise

    async def clear(self, agent_id: str, session_id: str):
        """Clear agent state from KV store."""
        dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS_DUMP_TYPE
        )
        blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS
        )
        # Use batch_delete for multiple keys
        deleted = await self._kv_store.batch_delete([dump_type_key, blob_key])
        session_logger.debug(
            "Agent checkpoint cleared",
            event_type=LogEventType.CHECKPOINT_CLEAR,
            session_id=session_id,
            agent_id=agent_id,
            metadata={"deleted_keys": deleted, "storage_type": "persistence"}
        )

    async def exists(self, session: BaseSession) -> bool:
        """Check if agent state exists in KV store."""
        session_id = session.session_id()
        agent_id = session.agent_id()

        pipeline = self._kv_store.pipeline()
        dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS_DUMP_TYPE
        )
        blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_AGENT, agent_id, self._STATE_BLOBS
        )
        await pipeline.exists(dump_type_key)
        await pipeline.exists(blob_key)
        results = await pipeline.execute()

        if len(results) != self._KEY_NUMS:
            return False

        # Both keys must exist for the state to be considered existing
        return results[0] is True and results[1] is True


class WorkflowStorage(BaseStorage):
    """Workflow state storage using BaseKVStore."""

    _STATE_BLOBS = "workflow_state_blobs"
    _STATE_BLOBS_DUMP_TYPE = "workflow_state_blobs_dump_type"
    _UPDATE_BLOBS = "workflow_update_blobs"
    _UPDATE_BLOBS_DUMP_TYPE = "workflow_update_blobs_dump_type"
    _KEY_NUMS = 4

    def _process_interactive_inputs(self, session: BaseSession, inputs: InteractiveInput) -> None:
        """Process interactive inputs and update workflow state."""
        if inputs.raw_inputs is not None:
            session.state().update_and_commit_workflow_state({INTERACTIVE_INPUT: inputs.raw_inputs})
            return

        if not (hasattr(inputs, 'user_inputs') and inputs.user_inputs):
            return

        for node_id, value in inputs.user_inputs.items():
            node_session = NodeSession(session, node_id)
            interactive_input = node_session.state().get(INTERACTIVE_INPUT)
            if isinstance(interactive_input, list):
                interactive_input.append(value)
                node_session.state().update({INTERACTIVE_INPUT: interactive_input})
            else:
                node_session.state().update({INTERACTIVE_INPUT: [value]})
        session.state().commit()

    async def save(self, session: BaseSession):
        """Save workflow state to KV store."""
        state = session.state().get_state()
        workflow_id = session.workflow_id()
        session_id = session.session_id()

        pipeline = self._kv_store.pipeline()
        has_operations = False

        state_blob = self._serialize_state(state)
        if state_blob:
            dump_type, blob = state_blob
            dump_type_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS_DUMP_TYPE
            )
            blob_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS
            )
            await pipeline.set(dump_type_key, dump_type)
            await pipeline.set(blob_key, blob)
            has_operations = True
        else:
            session_logger.warning(
                "Failed to serialize workflow state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                workflow_id=workflow_id,
                metadata={"operation": "serialize"}
            )

        updates = session.state().get_updates()
        updates_blob = self._serialize_state(updates)
        if updates_blob:
            dump_type, blob = updates_blob
            dump_type_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS_DUMP_TYPE
            )
            blob_key = build_key_with_namespace(
                session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS
            )
            await pipeline.set(dump_type_key, dump_type)
            await pipeline.set(blob_key, blob)
            has_operations = True

        if has_operations:
            try:
                await pipeline.execute()
                session_logger.debug(
                    "Workflow state saved successfully",
                    event_type=LogEventType.CHECKPOINT_SAVE,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    metadata={"storage_type": "persistence"}
                )
            except Exception as e:
                session_logger.error(
                    "Failed to save workflow state",
                    event_type=LogEventType.CHECKPOINT_ERROR,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    error_message=str(e),
                    metadata={"operation": "save", "storage_type": "persistence"}
                )
                raise

    async def recover(self, session: BaseSession, inputs: InteractiveInput = None):
        """Recover workflow state from KV store."""
        workflow_id = session.workflow_id()
        session_id = session.session_id()

        pipeline = self._kv_store.pipeline()
        state_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS_DUMP_TYPE
        )
        state_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS
        )
        await pipeline.get(state_dump_type_key)
        await pipeline.get(state_blob_key)
        updates_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS_DUMP_TYPE
        )
        updates_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS
        )
        await pipeline.get(updates_dump_type_key)
        await pipeline.get(updates_blob_key)
        results = await pipeline.execute()

        if len(results) != self._KEY_NUMS:
            session_logger.warning(
                "Unexpected key count during workflow state recovery",
                event_type=LogEventType.CHECKPOINT_RESTORE,
                session_id=session_id,
                workflow_id=workflow_id,
                metadata={"expected_keys": self._KEY_NUMS, "actual_keys": len(results)}
            )
            return

        # Recover state
        state_dump_type, state_blob = results[0], results[1]
        state_dump_type_str = self._decode_dump_type(state_dump_type)

        if state_blob and state_dump_type_str and state_dump_type_str != "empty":
            try:
                state = self._deserialize_state(state_dump_type_str, state_blob)
                if state is not None:
                    session.state().set_state(state)
            except Exception as e:
                session_logger.error(
                    "Failed to deserialize workflow state",
                    event_type=LogEventType.CHECKPOINT_ERROR,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    error_message=str(e),
                    metadata={"operation": "deserialize_state"}
                )

        # Process interactive inputs
        if inputs is not None:
            self._process_interactive_inputs(session, inputs)

        # Recover updates
        updates_dump_type, updates_blob = results[2], results[3]
        updates_dump_type_str = self._decode_dump_type(updates_dump_type)

        if updates_blob and updates_dump_type_str and updates_dump_type_str != "empty":
            try:
                state_updates = self._deserialize_state(updates_dump_type_str, updates_blob)
                if state_updates is not None:
                    session.state().set_updates(state_updates)
            except Exception as e:
                session_logger.error(
                    "Failed to deserialize workflow updates",
                    event_type=LogEventType.CHECKPOINT_ERROR,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    error_message=str(e),
                    metadata={"operation": "deserialize_updates"}
                )

    async def clear(self, workflow_id: str, session_id: str):
        """Clear workflow state from KV store."""
        state_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS_DUMP_TYPE
        )
        state_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS
        )
        state_updates_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS_DUMP_TYPE
        )
        state_updates_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS
        )
        # Use batch_delete for multiple keys
        deleted = await self._kv_store.batch_delete([
            state_dump_type_key, state_blob_key,
            state_updates_dump_type_key, state_updates_blob_key
        ])
        session_logger.debug(
            "Workflow checkpoint cleared",
            event_type=LogEventType.CHECKPOINT_CLEAR,
            session_id=session_id,
            workflow_id=workflow_id,
            metadata={"deleted_keys": deleted, "storage_type": "persistence"}
        )

    async def exists(self, session: BaseSession) -> bool:
        """Check if workflow state exists in KV store."""
        workflow_id = session.workflow_id()
        session_id = session.session_id()

        pipeline = self._kv_store.pipeline()
        # Check state keys
        state_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS_DUMP_TYPE
        )
        state_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._STATE_BLOBS
        )
        await pipeline.exists(state_dump_type_key)
        await pipeline.exists(state_blob_key)
        # Check updates keys (optional)
        state_updates_dump_type_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS_DUMP_TYPE
        )
        state_updates_blob_key = build_key_with_namespace(
            session_id, SESSION_NAMESPACE_WORKFLOW, workflow_id, self._UPDATE_BLOBS
        )
        await pipeline.exists(state_updates_dump_type_key)
        await pipeline.exists(state_updates_blob_key)
        results = await pipeline.execute()

        if len(results) != self._KEY_NUMS:
            return False

        # At least state keys must exist for the workflow state to be considered existing
        # Updates are optional, so we only require state keys to exist
        return results[0] is True and results[1] is True


class GraphStore(Store):
    """
    Graph state store implementation using BaseKVStore.
    
    Graph state keys are structured as: session:workflow-graph:workflow_id:suffix
    This separates graph state from workflow's own state.
    """

    _DATA_TYPE = "checkpoint_data_type"
    _DATA_VALUE = "checkpoint_data_value"
    _KEY_NUMS = 2

    def __init__(self, kv_store: BaseKVStore) -> None:
        """
        Initialize GraphStore with a BaseKVStore instance.

        Args:
            kv_store (BaseKVStore): The BaseKVStore instance for all storage operations.
        """
        self._kv_store = kv_store
        self._serde: Serializer = create_serializer("pickle")

    def _serialize_graph_state(self, graph_state: GraphState) -> Optional[Tuple[str, bytes]]:
        """Serialize graph state and return (dump_type, blob) tuple."""
        return self._serde.dumps_typed(graph_state)

    def _decode_dump_type(self, dump_type: Any) -> str:
        """Decode dump_type from bytes to string if needed."""
        if isinstance(dump_type, bytes):
            return dump_type.decode("utf-8")
        return dump_type if dump_type is not None else ""

    def _deserialize_graph_state(self, dump_type: Any, blob: Any) -> Optional[GraphState]:
        """Deserialize graph state from (dump_type, blob) tuple."""
        if not dump_type or blob is None:
            return None
        dump_type_str = self._decode_dump_type(dump_type)
        try:
            # Convert blob string back to bytes if needed
            if isinstance(blob, str):
                blob_bytes = base64.b64decode(blob)
            else:
                blob_bytes = blob
            return self._serde.loads_typed((dump_type_str, blob_bytes))
        except Exception as e:
            session_logger.error(
                "Failed to deserialize graph state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                error_message=str(e),
                metadata={"operation": "deserialize"}
            )
            return None

    async def get(self, session_id: str, ns: str) -> Optional[GraphState]:
        """Get graph state from KV store."""
        pipeline = self._kv_store.pipeline()
        key_type = build_key_with_namespace(
            session_id, WORKFLOW_NAMESPACE_GRAPH, ns, self._DATA_TYPE
        )
        await pipeline.get(key_type)
        key_value = build_key_with_namespace(
            session_id, WORKFLOW_NAMESPACE_GRAPH, ns, self._DATA_VALUE
        )
        await pipeline.get(key_value)
        results = await pipeline.execute()

        if len(results) != self._KEY_NUMS:
            session_logger.error(
                "Unexpected key count during graph state retrieval",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                metadata={"expected_keys": self._KEY_NUMS, "actual_keys": len(results), "namespace": ns}
            )
            return None

        _type, _value = results[0], results[1]
        if not _type or not _value:
            session_logger.debug(
                "Graph state not found in KV store",
                event_type=LogEventType.CHECKPOINT_RESTORE,
                session_id=session_id,
                metadata={"namespace": ns, "has_type": bool(_type), "has_value": bool(_value)}
            )
            return None

        graph_state = self._deserialize_graph_state(_type, _value)
        if graph_state is None:
            session_logger.debug(
                "Failed to deserialize graph state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                metadata={"namespace": ns}
            )
            return None
        return graph_state

    async def save(self, session_id: str, ns: str, state: GraphState) -> None:
        """Save graph state to KV store."""
        serialized = self._serialize_graph_state(state)
        if not serialized:
            session_logger.warning(
                "Failed to serialize graph state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                metadata={"namespace": ns, "operation": "serialize"}
            )
            return

        dump_type, blob = serialized
        dump_type_str = dump_type if isinstance(dump_type, str) else dump_type.decode("utf-8")

        try:
            key_type = build_key_with_namespace(
                session_id, WORKFLOW_NAMESPACE_GRAPH, ns, self._DATA_TYPE
            )
            pipeline = self._kv_store.pipeline()
            await pipeline.set(key_type, dump_type_str)
            key_value = build_key_with_namespace(
                session_id, WORKFLOW_NAMESPACE_GRAPH, ns, self._DATA_VALUE
            )
            await pipeline.set(key_value, blob)
            await pipeline.execute()
            session_logger.debug(
                "Graph state saved successfully",
                event_type=LogEventType.CHECKPOINT_SAVE,
                session_id=session_id,
                metadata={"namespace": ns, "storage_type": "graph"}
            )
        except Exception as e:
            session_logger.error(
                "Failed to save graph state",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                error_message=str(e),
                metadata={"namespace": ns, "operation": "save", "storage_type": "graph"}
            )
            raise

    async def delete(self, session_id: str, ns: Optional[str] = None) -> None:
        """
        Delete graph state keys for the given session_id and namespace.
        
        Args:
            session_id: Session identifier.
            ns: Namespace identifier. If None or empty, deletes all graph state data
                for the session_id (all namespaces under this session).
        """
        if not ns:
            # Delete all graph state data for this session_id
            prefix = build_key(session_id, WORKFLOW_NAMESPACE_GRAPH)
            await self._kv_store.delete_by_prefix(prefix)
            session_logger.debug(
                "Graph checkpoint cleared for all namespaces",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                metadata={"storage_type": "graph"}
            )
        else:
            # Delete specific namespace
            prefix = build_key_with_namespace(
                session_id, WORKFLOW_NAMESPACE_GRAPH, ns
            )
            await self._kv_store.delete_by_prefix(prefix)
            session_logger.debug(
                "Graph checkpoint cleared",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                metadata={"namespace": ns, "storage_type": "graph"}
            )


class PersistenceCheckpointer(Checkpointer):
    """
    Persistence-based checkpointer implementation using BaseKVStore.
    
    This checkpointer uses BaseKVStore interface for persistent storage,
    supporting any KV store implementation (shelve, database, etc.).
    """

    def __init__(self, kv_store: BaseKVStore):
        """
        Initialize PersistenceCheckpointer with a BaseKVStore instance.

        Args:
            kv_store (BaseKVStore): The BaseKVStore instance for all storage operations.
        """
        self._kv_store = kv_store
        self._agent_storage = AgentStorage(kv_store)
        self._workflow_storage = WorkflowStorage(kv_store)
        self._graph_state = GraphStore(kv_store)

    async def pre_agent_execute(self, session: BaseSession, inputs):
        """Prepare agent execution by recovering agent state."""
        session_logger.info(
            "Agent checkpoint restore initiated",
            event_type=LogEventType.CHECKPOINT_RESTORE,
            session_id=session.session_id(),
            agent_id=session.agent_id(),
            metadata={"operation": "pre_execute", "storage_type": "persistence"}
        )
        await self._agent_storage.recover(session)
        if inputs is not None:
            session.state().update({INTERACTIVE_INPUT: [inputs]})

    async def interrupt_agent_execute(self, session: BaseSession):
        """Save agent state when interaction is required."""
        session_logger.info(
            "Agent checkpoint save on interrupt",
            event_type=LogEventType.CHECKPOINT_SAVE,
            session_id=session.session_id(),
            agent_id=session.agent_id(),
            metadata={"reason": "interaction_required", "storage_type": "persistence"}
        )
        await self._agent_storage.save(session)

    async def post_agent_execute(self, session: BaseSession):
        """Save agent state after execution completes."""
        session_logger.info(
            "Agent checkpoint save on completion",
            event_type=LogEventType.CHECKPOINT_SAVE,
            session_id=session.session_id(),
            agent_id=session.agent_id(),
            metadata={"reason": "agent_finished", "storage_type": "persistence"}
        )
        await self._agent_storage.save(session)

    async def pre_workflow_execute(self, session: BaseSession, inputs: InteractiveInput):
        """
        Prepare workflow execution by recovering or clearing workflow state.
        
        If inputs is an InteractiveInput, recover the workflow state.
        If inputs is not an InteractiveInput and workflow state exists:
            - If FORCE_DEL_WORKFLOW_STATE_KEY is True, delete graph state and workflow state
            - Otherwise, raise WORKFLOW_STATE_INVALID exception
        
        Args:
            session (BaseSession): The session for the workflow.
            inputs (InteractiveInput): The input for the workflow execution.
        """
        workflow_id = session.workflow_id()
        session_logger.info(
            "Workflow checkpoint restore initiated",
            event_type=LogEventType.CHECKPOINT_RESTORE,
            session_id=session.session_id(),
            workflow_id=workflow_id,
            metadata={"operation": "pre_execute", "storage_type": "persistence"}
        )
        if isinstance(inputs, InteractiveInput):
            await self._workflow_storage.recover(session, inputs)
        else:
            # Check if workflow state exists
            if not await self._workflow_storage.exists(session):
                return

            # If FORCE_DEL_WORKFLOW_STATE_KEY is enabled, delete the state
            if session.config().get_env(FORCE_DEL_WORKFLOW_STATE_KEY, False):
                workflow_id = session.workflow_id()
                if workflow_id is None:
                    session_logger.warning(
                        "Workflow ID is None during state cleanup",
                        event_type=LogEventType.CHECKPOINT_ERROR,
                        session_id=session.session_id(),
                        metadata={"operation": "force_delete"}
                    )
                    return
                session_id = session.session_id()
                await self._graph_state.delete(session_id, workflow_id)
                await self._workflow_storage.clear(workflow_id, session_id)
                session_logger.info(
                    "Workflow state force deleted",
                    event_type=LogEventType.CHECKPOINT_CLEAR,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    metadata={"reason": "force_delete", "storage_type": "persistence"}
                )
            else:
                # Raise exception if state exists but cleanup is disabled
                raise build_error(
                    StatusCode.CHECKPOINTER_PRE_WORKFLOW_EXECUTION_ERROR,
                    workflow=workflow_id,
                    reason="workflow state exists but non-interactive input and cleanup is disabled"
                )

    async def post_workflow_execute(self, session: BaseSession, result, exception):
        """Handle workflow execution completion."""
        workflow_id = session.workflow_id()
        session_id = session.session_id()

        if exception is not None:
            session_logger.info(
                "Workflow checkpoint save on exception",
                event_type=LogEventType.CHECKPOINT_SAVE,
                session_id=session_id,
                workflow_id=workflow_id,
                metadata={"reason": "exception", "storage_type": "persistence"}
            )
            await self._workflow_storage.save(session)
            raise exception

        if result.get(TASK_STATUS_INTERRUPT) is None:
            session_logger.info(
                "Workflow checkpoint cleared on completion",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                workflow_id=workflow_id,
                metadata={"reason": "workflow_completed", "storage_type": "persistence"}
            )
            await self._graph_state.delete(session_id, workflow_id)
            await self._workflow_storage.clear(workflow_id, session_id)
        else:
            session_logger.info(
                "Workflow checkpoint save on interrupt",
                event_type=LogEventType.CHECKPOINT_SAVE,
                session_id=session_id,
                workflow_id=workflow_id,
                metadata={"reason": "interaction_required", "storage_type": "persistence"}
            )
            await self._workflow_storage.save(session)

    async def session_exists(self, session_id: str) -> bool:
        """
        Check if a session exists in KV store.
        
        Args:
            session_id (str): The session ID to check.
        
        Returns:
            bool: True if the session exists (has associated keys), False otherwise.
        """
        if self._kv_store is None:
            return False

        # Check if any keys exist with the session_id prefix
        prefix = f"{session_id}:"
        keys = await self._kv_store.get_by_prefix(prefix)
        return len(keys) > 0

    async def release(self, session_id: str, agent_id: Optional[str] = None):
        """
        Release resources for a session, optionally for a specific agent.
        
        Args:
            session_id (str): The session ID to release resources for.
            agent_id (str, optional): If provided, only release resources for this specific agent.
        """
        if self._kv_store is None:
            session_logger.warning(
                "Cannot release resources: KV store is None",
                event_type=LogEventType.CHECKPOINT_ERROR,
                session_id=session_id,
                metadata={"operation": "release"}
            )
            return

        if agent_id is not None:
            session_logger.info(
                "Agent checkpoint cleared",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                agent_id=agent_id,
                metadata={"operation": "release", "storage_type": "persistence"}
            )
            await self._agent_storage.clear(agent_id, session_id)
        else:
            session_logger.info(
                "Session cleared",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                metadata={"operation": "release_all", "storage_type": "persistence"}
            )
            # Delete all keys matching the session prefix
            prefix = f"{session_id}:"
            await self._kv_store.delete_by_prefix(prefix)
            session_logger.debug(
                "All session resources released",
                event_type=LogEventType.CHECKPOINT_CLEAR,
                session_id=session_id,
                metadata={"storage_type": "persistence"}
            )

    def graph_store(self) -> Store:
        """Return the graph store instance."""
        return self._graph_state


@CheckpointerFactory.register("persistence")
class PersistenceCheckpointerProvider(CheckpointerProvider):
    """
    Provider for creating persistence-based checkpointers.
    
    Configuration format:
        {
            "db_type": "sqlite" | "shelve",  # Optional: Type of storage backend (default: "sqlite")
            "db_path": "checkpointer.db",     # Optional: Path to database file
            "db_client": AsyncEngine          # Optional: Pre-configured database engine
        }
    """

    async def create(self, conf: dict) -> Checkpointer:
        """
        Create a PersistenceCheckpointer instance.
        
        Args:
            conf (dict): Configuration dictionary with optional keys:
                - 'db_type': Type of storage backend ("sqlite" or "shelve", default: "sqlite")
                - 'db_path': Path to database file (default: "checkpointer")
                - 'db_client': Pre-configured AsyncEngine instance (optional)
        
        Returns:
            Checkpointer: A PersistenceCheckpointer instance.
        """
        db_type = conf.get("db_type", "sqlite")
        db_path = conf.get("db_path", "checkpointer")

        if db_type == "sqlite":
            db_client = conf.get("db_client")
            if db_client is not None and isinstance(db_client, AsyncEngine):
                kv_store = DbBasedKVStore(db_client)
            else:
                if not db_path.endswith(".db"):
                    db_path = f"{db_path}.db"
                # Ensure parent directory exists; SQLite cannot create it.
                if db_path and not db_path.strip().startswith(":memory:"):
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
                kv_store = DbBasedKVStore(engine)
        elif db_type == "shelve":
            if db_path.endswith(".db"):
                db_path = db_path.removesuffix(".db")
            kv_store = ShelveStore(db_path)
        else:
            raise build_error(StatusCode.CHECKPOINTER_CONFIG_ERROR, reason=f"db type[{db_type}] is not supported")
        return PersistenceCheckpointer(kv_store)

"""
Checkpoint serializer with an explicit msgpack allowlist for kernel state types.

langgraph-checkpoint 4.x warns (and will later refuse) when deserializing
unregistered classes from checkpoints. Everything stored in ``AgentState``
must be listed here - tests use this serializer, so a missing type fails CI.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .. import state as _state

_STATE_TYPES = (
    _state.RunStatus,
    _state.TaskStatus,
    _state.VerificationGateStatus,
    _state.NodeName,
    _state.InterruptType,
    _state.TokenUsage,
    _state.FileChange,
    _state.VerificationGateResult,
    _state.Task,
    _state.ArtifactMetadata,
)

KERNEL_MSGPACK_TYPES: tuple[tuple[str, str], ...] = tuple((t.__module__, t.__qualname__) for t in _STATE_TYPES)


def kernel_serde(extra: tuple[tuple[str, str], ...] = ()) -> JsonPlusSerializer:
    """Serializer allowing kernel state types plus ``extra`` (e.g. vertical-specific metadata models)."""
    return JsonPlusSerializer(allowed_msgpack_modules=[*KERNEL_MSGPACK_TYPES, *extra])

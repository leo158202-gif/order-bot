"""Transport-independent contracts. Context always originates in trusted code."""
from dataclasses import dataclass, field
from typing import Any, TypedDict


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    task_id: str
    request_id: str
    target_id: str | None = None
    capability_id: str | None = None
    evidence_group_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    state_version: str = "initial"
    deadline: float | None = None  # Unix seconds; persisted request deadline.


class ToolResult(TypedDict):
    status: str
    data: Any
    evidence_refs: list[str]
    error: dict | None
    retryable: bool


class TaskSnapshot(TypedDict, total=False):
    task_id: str
    status: str
    phase: str
    events: list[dict]
    result: dict | None
    error: dict | None
    can_cancel: bool
    messages: list[dict]


class RepairManifest(TypedDict):
    action_id: str
    evidence_refs: list[str]
    target: dict
    before: Any
    after: Any
    base_hashes: dict[str, str]
    impact: dict
    tests: list[dict]


class WorkerInput(TypedDict):
    operation: str
    sources: list[dict]
    model_response: dict
    rule_version: str
    prompt_version: str


class WorkerOutput(TypedDict):
    items: list[dict]
    explanation: str
    worker_version: str


def tool_result(status="ok", data=None, *, evidence_refs=None, error=None, retryable=False) -> ToolResult:
    return {"status": status, "data": {} if data is None else data,
            "evidence_refs": list(evidence_refs or []), "error": error, "retryable": bool(retryable)}

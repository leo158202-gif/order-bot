"""Durable per-request admission limits shared by UI and tool transports."""

MODEL_CALL_LIMIT = 8
TOOL_CALL_LIMIT = 12
ATTEMPT_LIMIT = 3


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetGuard:
    def __init__(self, store, session_id: str, task_id: str):
        self.store = store
        self.session_id = session_id
        self.task_id = task_id

    def consume(self, kind: str, operation_id: str) -> dict:
        """Admit before external work. Replayed operations must resume, not rerun."""
        result = self.store.consume_budget(self.session_id, self.task_id, kind, operation_id)
        if not result["allowed"]:
            raise BudgetExceeded(result["reason"])
        return result


"""Small, injected-path SQLite ledger. No imports of the order database.

Business evidence identities must come from the trusted matching service, never
from a model supplied file name, report name, conversation or code version.
The store accounts for admission; callers still must enforce that admission
before model/tool work and use operation records to recover interrupted work.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .budget import ATTEMPT_LIMIT, MODEL_CALL_LIMIT, TOOL_CALL_LIMIT


KNOWN_STOP_GOLD_SHA = "18a3ddb3ea0d1d46b32ffc48d3d23318dd6a64e17b714f0b886772f49834012e"
KNOWN_STOP_REFS = [
    "docs/问题清单.md#三轮停点与下一项用户2026-10-26-1912明确",
    "data/day-execution-20261026/after-offline-comparison-v8.json",
]
_STATUSES = {"queued", "running", "done", "failed", "cancelled", "needs_input", "parked", "partial"}
_FINISHED = {"done", "failed", "cancelled", "needs_input", "parked", "partial"}
_PUBLIC_PHASES = {"", "queued", "running", "thinking", "checking", "answering", "model", "tool",
                  "done", "failed", "cancelled", "needs_input", "parked", "partial", "preparing",
                  "validating", "applying", "verifying", "completed", "budget_exhausted"}
_TABLES = {"doctor_meta", "conversations", "messages", "tasks", "budget_operations", "events",
           "records", "evidence_groups", "attempt_requests", "known_stops"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _required(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{name} must be a nonempty string (max 1000 characters)")
    return value


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("payload", "result", "evidence_refs", "scope"):
        if key in result:
            result[key] = json.loads(result[key]) if result[key] is not None else None
    if "cancel_requested" in result:
        result["cancel_requested"] = bool(result["cancel_requested"])
    return result


class DoctorStore:
    """Connections are short lived; BEGIN IMMEDIATE serializes all admissions."""

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("DoctorStore requires a persistent independent database path")
        self.path = Path(path).resolve()
        production = Path(__file__).resolve().parents[2] / "data" / "db.sqlite"
        if self.path == production.resolve() or (self.path.exists() and production.exists()
                                               and self.path.samefile(production)):
            raise ValueError("DoctorStore requires an independent database, not the business database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - _TABLES:
                raise ValueError("DoctorStore requires an independent database; existing tables were not changed")
            # DDL runs inside the same transaction as the ownership/schema check.
            statements = (
                "CREATE TABLE IF NOT EXISTS doctor_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),role TEXT NOT NULL,content TEXT NOT NULL,request_id TEXT,created_at TEXT NOT NULL,UNIQUE(conversation_id,request_id))",
                "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,conversation_id TEXT NOT NULL REFERENCES conversations(id),request_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,phase TEXT NOT NULL DEFAULT '',result TEXT,error TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,model_calls INTEGER NOT NULL DEFAULT 0,tool_calls INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(session_id,request_id))",
                "CREATE TABLE IF NOT EXISTS budget_operations(task_id TEXT NOT NULL REFERENCES tasks(id),kind TEXT NOT NULL,operation_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(task_id,kind,operation_id))",
                "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS records(session_id TEXT NOT NULL,namespace TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,task_id TEXT REFERENCES tasks(id),revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(session_id,namespace,key))",
                "CREATE TABLE IF NOT EXISTS evidence_groups(evidence_key TEXT PRIMARY KEY,issue_id TEXT NOT NULL,source_digest TEXT NOT NULL,gold_digest TEXT NOT NULL,scope TEXT NOT NULL,attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 3),status TEXT NOT NULL,reason TEXT NOT NULL,evidence_refs TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS attempt_requests(issue_id TEXT NOT NULL,request_id TEXT NOT NULL,evidence_key TEXT NOT NULL REFERENCES evidence_groups(evidence_key),attempt INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(issue_id,request_id))",
                "CREATE TABLE IF NOT EXISTS known_stops(gold_sha TEXT NOT NULL,source_queue_id TEXT NOT NULL,evidence_refs TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(gold_sha,source_queue_id))",
            )
            for statement in statements:
                connection.execute(statement)
            version = connection.execute("SELECT value FROM doctor_meta WHERE key='schema_version'").fetchone()
            if version and version[0] != "1":
                raise ValueError("unsupported diagnostician ledger schema version")
            connection.execute("INSERT OR IGNORE INTO doctor_meta VALUES('schema_version','1')")
            connection.execute("INSERT OR IGNORE INTO known_stops VALUES(?,?,?,?)",
                               (KNOWN_STOP_GOLD_SHA, "*", _json(KNOWN_STOP_REFS), _now()))

    @contextmanager
    def _connection(self, write=False):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _owned(connection, table, session_id, identity):
        # Table names are fixed internal literals, never caller/model strings.
        row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND session_id=?",
                                 (identity, session_id)).fetchone()
        if row is None:
            raise PermissionError("resource is unavailable in this session")
        return row

    def ensure_conversation(self, session_id, conversation_id=None):
        _required(session_id, "session_id")
        with self._connection(write=True) as connection:
            if conversation_id is not None:
                return _row(self._owned(connection, "conversations", session_id, conversation_id))
            identity = uuid.uuid4().hex
            connection.execute("INSERT INTO conversations VALUES(?,?,?)", (identity, session_id, _now()))
            return _row(connection.execute("SELECT * FROM conversations WHERE id=?", (identity,)).fetchone())

    def get_conversation(self, session_id, conversation_id):
        with self._connection() as connection:
            return _row(connection.execute("SELECT * FROM conversations WHERE id=? AND session_id=?",
                                           (conversation_id, session_id)).fetchone())

    def append_message(self, session_id, conversation_id, role, content, request_id=None):
        if role not in {"user", "assistant", "system", "tool"} or not isinstance(content, str):
            raise ValueError("invalid message")
        if request_id is not None:
            _required(request_id, "request_id")
        with self._connection(write=True) as connection:
            self._owned(connection, "conversations", session_id, conversation_id)
            old = connection.execute("SELECT * FROM messages WHERE conversation_id=? AND request_id=?",
                                     (conversation_id, request_id)).fetchone() if request_id else None
            if old:
                if old["role"] != role or old["content"] != content:
                    raise ValueError("message request_id already has different content")
                return _row(old)
            identity = uuid.uuid4().hex
            connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                               (identity, conversation_id, role, content, request_id, _now()))
            return _row(connection.execute("SELECT * FROM messages WHERE id=?", (identity,)).fetchone())

    def list_messages(self, session_id, conversation_id, limit=200):
        with self._connection() as connection:
            self._owned(connection, "conversations", session_id, conversation_id)
            rows = connection.execute("SELECT * FROM (SELECT rowid AS position,* FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?) ORDER BY position",
                                      (conversation_id, self._limit(limit, 1000))).fetchall()
            return [{key: value for key, value in _row(row).items() if key != "position"} for row in rows]

    @staticmethod
    def _limit(value, maximum):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return value

    def create_task(self, session_id, conversation_id, request_id, *, kind="chat", payload=None):
        _required(request_id, "request_id")
        _required(kind, "kind")
        encoded = _json(payload if payload is not None else {})
        with self._connection(write=True) as connection:
            self._owned(connection, "conversations", session_id, conversation_id)
            old = connection.execute("SELECT * FROM tasks WHERE session_id=? AND request_id=?",
                                     (session_id, request_id)).fetchone()
            if old:
                if old["conversation_id"] != conversation_id or old["kind"] != kind or old["payload"] != encoded:
                    raise ValueError("task request_id already has different immutable input")
                return _row(old)
            identity, now = uuid.uuid4().hex, _now()
            connection.execute("INSERT INTO tasks(id,session_id,conversation_id,request_id,kind,payload,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
                               (identity, session_id, conversation_id, request_id, kind, encoded, now, now))
            self._event(connection, identity, "task.created", {"kind": kind, "request_id": request_id})
            return _row(connection.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone())

    def get_task(self, session_id, task_id):
        with self._connection() as connection:
            return _row(connection.execute("SELECT * FROM tasks WHERE id=? AND session_id=?", (task_id, session_id)).fetchone())

    def list_pending_tasks(self, limit=200):
        """Trusted startup recovery only; never expose this across HTTP/MCP."""
        with self._connection() as connection:
            return [_row(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY rowid LIMIT ?",
                (self._limit(limit, 1000),)).fetchall()]

    def list_tasks(self, session_id, conversation_id=None, limit=20):
        with self._connection() as connection:
            if conversation_id is not None:
                self._owned(connection, "conversations", session_id, conversation_id)
                rows = connection.execute("SELECT * FROM tasks WHERE session_id=? AND conversation_id=? ORDER BY rowid DESC LIMIT ?",
                                          (session_id, conversation_id, self._limit(limit, 200))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
                                          (session_id, self._limit(limit, 200))).fetchall()
            return [_row(row) for row in rows]

    def update_task(self, session_id, task_id, *, status=None, phase=None, result=None, error=None):
        if status is not None and status not in _STATUSES:
            raise ValueError("invalid task status")
        if phase is not None and (not isinstance(phase, str) or len(phase) > 200):
            raise ValueError("invalid task phase")
        with self._connection(write=True) as connection:
            old = self._owned(connection, "tasks", session_id, task_id)
            if old["cancel_requested"] or old["status"] in _FINISHED:
                return _row(old)
            fields, values = [], []
            for key, value in (("status", status), ("phase", phase), ("result", _json(result) if result is not None else None), ("error", str(error) if error is not None else None)):
                if value is not None:
                    fields.append(f"{key}=?")
                    values.append(value)
            if fields:
                connection.execute(f"UPDATE tasks SET {','.join(fields)},updated_at=? WHERE id=?", (*values, _now(), task_id))
                self._event(connection, task_id, "task.updated", {"status": status or old["status"], "phase": phase if phase is not None else old["phase"]})
            return _row(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def cancel_task(self, session_id, task_id):
        with self._connection(write=True) as connection:
            old = self._owned(connection, "tasks", session_id, task_id)
            if old["status"] in _FINISHED:
                return _row(old)
            connection.execute("UPDATE tasks SET cancel_requested=1,status='cancelled',updated_at=? WHERE id=?", (_now(), task_id))
            self._event(connection, task_id, "task.cancelled", {})
            return _row(connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_progress(self, session_id, task_id):
        task = self.get_task(session_id, task_id)
        if task is None:
            return None
        # No free-form payload, result or error is a public progress field.
        fields = ("id", "conversation_id", "status", "cancel_requested", "model_calls", "tool_calls", "created_at", "updated_at")
        result = {field: task[field] for field in fields}
        # Phases are UI labels, not an error dump or user/model text channel.
        phase = task["phase"]
        result["phase"] = phase if phase in _PUBLIC_PHASES else ""
        result.update(max_model_calls=MODEL_CALL_LIMIT, max_tool_calls=TOOL_CALL_LIMIT)
        return result

    def consume_budget(self, session_id, task_id, kind, operation_id):
        if kind not in {"model", "tool"}:
            raise ValueError("budget kind must be model or tool")
        _required(operation_id, "operation_id")
        field, limit = ("model_calls", MODEL_CALL_LIMIT) if kind == "model" else ("tool_calls", TOOL_CALL_LIMIT)
        with self._connection(write=True) as connection:
            task = self._owned(connection, "tasks", session_id, task_id)
            base = {"allowed": False, "replayed": False, "used": task[field], "limit": limit}
            if task["cancel_requested"]:
                return {**base, "reason": "cancelled"}
            if task["status"] in _FINISHED:
                return {**base, "reason": "task_finished"}
            previous = connection.execute("SELECT 1 FROM budget_operations WHERE task_id=? AND kind=? AND operation_id=?", (task_id, kind, operation_id)).fetchone()
            if previous:
                return {**base, "allowed": True, "replayed": True, "reason": ""}
            if task[field] >= limit:
                return {**base, "reason": "budget_exhausted"}
            connection.execute("INSERT INTO budget_operations VALUES(?,?,?,?)", (task_id, kind, operation_id, _now()))
            connection.execute(f"UPDATE tasks SET {field}={field}+1,updated_at=? WHERE id=?", (_now(), task_id))
            self._event(connection, task_id, "budget.admitted", {"kind": kind, "operation_id": operation_id, "used": task[field] + 1})
            return {**base, "allowed": True, "used": task[field] + 1, "reason": ""}

    @staticmethod
    def _event(connection, task_id, kind, payload):
        cursor = connection.execute("INSERT INTO events(task_id,kind,payload,created_at) VALUES(?,?,?,?)", (task_id, kind, _json(payload), _now()))
        return _row(connection.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone())

    def append_event(self, session_id, task_id, kind, payload):
        _required(kind, "kind")
        with self._connection(write=True) as connection:
            self._owned(connection, "tasks", session_id, task_id)
            return self._event(connection, task_id, kind, payload)

    def list_events(self, session_id, task_id, limit=200):
        with self._connection() as connection:
            self._owned(connection, "tasks", session_id, task_id)
            return [_row(row) for row in connection.execute("SELECT * FROM (SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (task_id, self._limit(limit, 1000))).fetchall()]

    def put_record(self, session_id, namespace, key, payload, *, task_id=None, expected_revision=None):
        for value, name in ((session_id, "session_id"), (namespace, "namespace"), (key, "key")):
            _required(value, name)
        encoded = _json(payload)
        with self._connection(write=True) as connection:
            if task_id is not None:
                self._owned(connection, "tasks", session_id, task_id)
            old = connection.execute("SELECT * FROM records WHERE session_id=? AND namespace=? AND key=?", (session_id, namespace, key)).fetchone()
            revision = old["revision"] if old else 0
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("record revision conflict")
            if old and old["payload"] == encoded and (task_id is None or task_id == old["task_id"]):
                return _row(old)
            now = _now()
            connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,namespace,key) DO UPDATE SET payload=excluded.payload,task_id=COALESCE(excluded.task_id,records.task_id),revision=excluded.revision,updated_at=excluded.updated_at",
                               (session_id, namespace, key, encoded, task_id, revision + 1, old["created_at"] if old else now, now))
            if task_id:
                self._event(connection, task_id, "record.updated", {"namespace": namespace, "key": key, "revision": revision + 1})
            return _row(connection.execute("SELECT * FROM records WHERE session_id=? AND namespace=? AND key=?", (session_id, namespace, key)).fetchone())

    def get_record(self, session_id, namespace, key):
        with self._connection() as connection:
            return _row(connection.execute("SELECT * FROM records WHERE session_id=? AND namespace=? AND key=?", (session_id, namespace, key)).fetchone())

    def pending_feedback(self, session_id, conversation_id, *, limit=10, sheet=None, cell=None):
        """Read conflict facts from the ledger, independent of task history size.

        Owner-task membership supplies conversation identity. Optional physical
        position filters apply before counting/limiting, never after truncation.
        """
        bounded = self._limit(limit, 50)
        if (sheet is None) != (cell is None):
            raise ValueError('sheet and cell must be supplied together')
        if sheet is not None and (not isinstance(sheet, str) or not 1 <= len(sheet) <= 200
                or not isinstance(cell, str) or not re.fullmatch(r'[A-I][1-9]\d{0,5}', cell)):
            raise ValueError('invalid feedback position')
        query = (" FROM records r JOIN tasks owner ON owner.id=json_extract(r.payload,'$.owner_task_id') "
                 "AND owner.session_id=r.session_id WHERE r.session_id=? AND owner.conversation_id=? "
                 "AND r.namespace='feedback' AND json_extract(r.payload,'$.phase')='conflict'")
        values = [session_id, conversation_id]
        if sheet is not None:
            query += (" AND json_extract(r.payload,'$.location.sheet_title')=? "
                      "AND json_extract(r.payload,'$.location.physical_cell')=?")
            values.extend((sheet, cell))
        with self._connection() as connection:
            self._owned(connection, 'conversations', session_id, conversation_id)
            total = connection.execute('SELECT count(*)' + query, values).fetchone()[0]
            rows = connection.execute('SELECT r.key,r.payload' + query +
                ' ORDER BY r.created_at DESC,r.key LIMIT ?', [*values, bounded]).fetchall()
            items = []
            for row in rows:
                payload = json.loads(row['payload']); location = payload.get('location', {})
                def display(value):
                    return str(value)[:160] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''
                items.append({'feedback_id': row['key'],
                    'location': {key: location.get(key) for key in ('order_id', 'target_id', 'sheet_title', 'physical_cell')},
                    'before': display(payload.get('before_value')), 'after': display(payload.get('after_value')),
                    'gold': display((payload.get('gold') or {}).get('value'))})
        return {'items': items, 'total_count': total, 'truncated': total > len(items)}

    def recovery_facts(self, session_id, conversation_id, *, limit=10):
        """Recent verified actions / accepted unprepared feedback, facts only.

        The feedback's immutable owner task supplies conversation membership;
        a later update task cannot move an old record into another conversation.
        No business database or model message history is read.
        """
        bounded = self._limit(limit, 20)
        membership = (" JOIN tasks owner ON owner.id=json_extract(f.payload,'$.owner_task_id') "
                      "AND owner.session_id=f.session_id ")
        accepted_query = (" FROM records f" + membership +
            "WHERE f.session_id=? AND owner.conversation_id=? AND f.namespace='feedback' "
            "AND json_extract(f.payload,'$.phase')='accepted' AND NOT EXISTS "
            "(SELECT 1 FROM records c WHERE c.session_id=f.session_id AND c.namespace='correction' "
            "AND json_extract(c.payload,'$.manifest.feedback_id')=f.key)")
        verified_query = (" FROM records c JOIN records f ON f.session_id=c.session_id "
            "AND f.namespace='feedback' AND f.key=json_extract(c.payload,'$.manifest.feedback_id')" +
            membership + "WHERE c.session_id=? AND owner.conversation_id=? AND c.namespace='correction' "
            "AND json_extract(c.payload,'$.phase')='verified' AND json_extract(f.payload,'$.phase')='accepted'")
        with self._connection() as connection:
            self._owned(connection, 'conversations', session_id, conversation_id)
            args = (session_id, conversation_id, bounded + 1)
            feedback = connection.execute('SELECT f.key,f.payload' + accepted_query +
                ' ORDER BY f.updated_at DESC,f.key LIMIT ?', args).fetchall()
            actions = connection.execute('SELECT c.key,c.payload' + verified_query +
                ' ORDER BY c.updated_at DESC,c.key LIMIT ?', args).fetchall()
        def display(value):
            return str(value)[:160] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''
        accepted, verified = [], []
        for row in feedback[:bounded]:
            value = json.loads(row['payload']); position = value.get('location') or {}
            accepted.append({'feedback_id': row['key'], 'phase': 'accepted',
                'order_id': position.get('order_id'), 'cell': position.get('physical_cell'),
                'sheet_title': position.get('sheet_title'), 'target_id': position.get('target_id'),
                'field': value.get('field'), 'after_value': display(value.get('after_value'))})
        for row in actions[:bounded]:
            state = json.loads(row['payload']); manifest = dict(state.get('manifest') or {})
            frozen_digest = manifest.pop('digest', None)
            if hashlib.sha256(_json(manifest).encode('utf-8')).hexdigest() != frozen_digest: continue
            cell, field = manifest.get('cell', ''), manifest.get('field')
            match = re.fullmatch(r'([B-I])([1-9]\d*)', cell) if isinstance(cell, str) else None
            if not match or manifest.get('action_id') != row['key']: continue
            try:
                data = manifest['after_data']
                if match[1] in 'BCD':
                    offset = int(match[2]) - manifest['before_checkpoint']['rows'][0]
                    if offset < 0: continue
                    after = data['lines'][offset][field]
                else: after = data.get(field, '')
            except (KeyError, TypeError, IndexError): continue
            verified.append({'action_id': row['key'], 'phase': 'verified', 'order_id': manifest.get('order_id'),
                'cell': cell, 'field': field, 'after_value': display(after), 'target_id': manifest.get('target_id')})
        return {'accepted_feedback': accepted, 'verified_corrections': verified,
                'accepted_feedback_truncated': len(feedback) > bounded,
                'verified_corrections_truncated': len(actions) > bounded}

    @staticmethod
    def _evidence_identity(issue_id, source_digest, gold_digest, scope):
        for value, name in ((issue_id, "issue_id"), (source_digest, "source_digest"), (gold_digest, "gold_digest")):
            _required(value, name)
        def check_scope(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if re.search(r"report|filename|file_name|file_sha|file_hash|code_version|script_version|session|conversation|mtime|metadata", str(key), re.I):
                        raise ValueError("volatile metadata cannot identify business evidence")
                    check_scope(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    check_scope(item)
        check_scope(scope)
        encoded_scope = _json(scope)
        identity = _json([issue_id, source_digest, gold_digest, scope])
        return hashlib.sha256(identity.encode("utf-8")).hexdigest(), encoded_scope

    @staticmethod
    def _group_result(row, *, allowed=False, replayed=False, attempt=None):
        result = _row(row)
        result.update(allowed=allowed, replayed=replayed, attempt=attempt)
        return result

    def begin_attempt(self, issue_id, source_digest, gold_digest, scope, request_id, *, historical=False,
                      evidence_refs=None, gold_file_sha=None, source_queue_id=None):
        """Reserve once before work. Only semantic per-item evidence joins its key.

        ``historical`` must be set by a trusted ingestion boundary whenever an
        item predates this ledger. Unknown history fails closed. Known file/queue
        guards are conservative imports, not evidence-key ingredients.
        """
        _required(request_id, "request_id")
        identity, encoded_scope = self._evidence_identity(issue_id, source_digest, gold_digest, scope)
        with self._connection(write=True) as connection:
            previous = connection.execute("SELECT * FROM attempt_requests WHERE issue_id=? AND request_id=?", (issue_id, request_id)).fetchone()
            if previous and previous["evidence_key"] != identity:
                raise ValueError("attempt request_id already belongs to other evidence")
            group = connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone()
            if group is None:
                known = connection.execute("SELECT * FROM known_stops WHERE gold_sha=? ORDER BY source_queue_id LIMIT 1", (gold_file_sha,)).fetchone() if gold_file_sha else None
                queue = connection.execute("SELECT * FROM known_stops WHERE source_queue_id=? LIMIT 1", (source_queue_id,)).fetchone() if source_queue_id else None
                # A previously captured per-item baseline lets a real supplement
                # reopen just this item even when its old workbook is retained.
                related_baseline = connection.execute("SELECT 1 FROM evidence_groups WHERE issue_id=? AND scope=? AND (source_digest<>? OR gold_digest<>?) LIMIT 1", (issue_id, encoded_scope, source_digest, gold_digest)).fetchone()
                if related_baseline:
                    known, queue, historical = None, None, False
                status = "parked_missing_evidence" if known else "parked_history_unknown" if historical or queue else "active"
                reason = "旧金标已累计核对3轮，等待本项实质新证据" if known else "历史次数或身份无法可靠恢复，等待本项新证据" if historical or queue else ""
                refs = evidence_refs or (json.loads((known or queue)["evidence_refs"]) if known or queue else [])
                now = _now()
                connection.execute("INSERT INTO evidence_groups VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, issue_id, source_digest, gold_digest, encoded_scope, ATTEMPT_LIMIT if known else 0, status, reason, _json(refs), now, now))
                group = connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone()
            if group["status"] != "active":
                return self._group_result(group)
            if previous:
                return self._group_result(group, allowed=True, replayed=True, attempt=previous["attempt"])
            if group["attempts"] >= ATTEMPT_LIMIT:
                connection.execute("UPDATE evidence_groups SET status='parked_missing_evidence',reason=?,updated_at=? WHERE evidence_key=?", ("本项已核对3轮，缺少新证据，保留待核备注", _now(), identity))
                return self._group_result(connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone())
            number = group["attempts"] + 1
            connection.execute("UPDATE evidence_groups SET attempts=?,updated_at=? WHERE evidence_key=?", (number, _now(), identity))
            connection.execute("INSERT INTO attempt_requests VALUES(?,?,?,?,?)", (issue_id, request_id, identity, number, _now()))
            return self._group_result(connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone(), allowed=True, attempt=number)

    def park_attempt(self, evidence_key, reason, *, evidence_refs=None):
        _required(reason, "reason")
        with self._connection(write=True) as connection:
            old = connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (evidence_key,)).fetchone()
            if old is None:
                raise KeyError("unknown evidence group")
            refs = _json(evidence_refs) if evidence_refs is not None else old["evidence_refs"]
            status = old["status"] if old["status"] == "parked_history_unknown" else "parked_missing_evidence"
            connection.execute("UPDATE evidence_groups SET status=?,reason=?,evidence_refs=?,updated_at=? WHERE evidence_key=?", (status, reason, refs, _now(), evidence_key))
            return self._group_result(connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (evidence_key,)).fetchone())

    def import_history(self, issue_id, source_digest, gold_digest, scope, *, attempts=None, evidence_refs=None):
        if attempts is not None and (type(attempts) is not int or not 0 <= attempts <= ATTEMPT_LIMIT):
            raise ValueError("history attempts must be 0..3 or None")
        identity, encoded_scope = self._evidence_identity(issue_id, source_digest, gold_digest, scope)
        with self._connection(write=True) as connection:
            old = connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone()
            number = max(attempts or 0, old["attempts"] if old else 0)
            unknown = attempts is None or (old and old["status"] == "parked_history_unknown")
            status = "parked_history_unknown" if unknown else "parked_missing_evidence" if number >= ATTEMPT_LIMIT else old["status"] if old else "active"
            reason = "历史次数或身份无法可靠恢复，等待本项新证据" if unknown else "历史已累计核对3轮，等待本项新证据" if number >= ATTEMPT_LIMIT else old["reason"] if old else ""
            refs = list(dict.fromkeys((json.loads(old["evidence_refs"]) if old else []) + (evidence_refs or [])))
            now = _now()
            connection.execute("INSERT INTO evidence_groups VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_key) DO UPDATE SET attempts=excluded.attempts,status=excluded.status,reason=excluded.reason,evidence_refs=excluded.evidence_refs,updated_at=excluded.updated_at",
                               (identity, issue_id, source_digest, gold_digest, encoded_scope, number, status, reason, _json(refs), old["created_at"] if old else now, now))
            return self._group_result(connection.execute("SELECT * FROM evidence_groups WHERE evidence_key=?", (identity,)).fetchone())

    def import_known_stop(self, gold_sha, source_queue_id="*", evidence_refs=None):
        _required(gold_sha, "gold_sha")
        _required(source_queue_id, "source_queue_id")
        with self._connection(write=True) as connection:
            old = connection.execute("SELECT * FROM known_stops WHERE gold_sha=? AND source_queue_id=?", (gold_sha, source_queue_id)).fetchone()
            refs = list(dict.fromkeys((json.loads(old["evidence_refs"]) if old else []) + (evidence_refs or [])))
            connection.execute("INSERT INTO known_stops VALUES(?,?,?,?) ON CONFLICT(gold_sha,source_queue_id) DO UPDATE SET evidence_refs=excluded.evidence_refs", (gold_sha, source_queue_id, _json(refs), old["created_at"] if old else _now()))
            return _row(connection.execute("SELECT * FROM known_stops WHERE gold_sha=? AND source_queue_id=?", (gold_sha, source_queue_id)).fetchone())

    def list_evidence_groups(self, issue_id):
        with self._connection() as connection:
            return [_row(row) for row in connection.execute("SELECT * FROM evidence_groups WHERE issue_id=? ORDER BY rowid", (issue_id,)).fetchall()]

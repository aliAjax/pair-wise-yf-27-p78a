"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
    # 来源撤回/更正后失效的主张，可由审查员退回 submitted 重算。
    "invalidated": {"submitted"},
}
SOURCE_UPDATE_KINDS = {"withdrawal", "correction"}
UNFINISHED_CLAIM_STATUS = ("submitted", "under_review", "negotiating")


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','withdrawn','corrected')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected','invalidated')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_updates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    kind TEXT NOT NULL CHECK(kind IN ('withdrawal','correction')),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending_confirmation'
                        CHECK(status IN ('pending_confirmation','pending_retry','applying','applied')),
                    scope TEXT NOT NULL, scope_token TEXT NOT NULL,
                    correction TEXT,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, applied_at TEXT
                );
                CREATE TABLE IF NOT EXISTS source_update_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER NOT NULL REFERENCES source_updates(id),
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
                    processed_at TEXT,
                    UNIQUE(update_id,object_id)
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _source(self, conn, source_id):
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise BusinessError("来源不存在", 404, "source_not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    # ---- 来源撤回/更正的影响核对 ----

    def _impact_scope(self, conn, source_id):
        """引用该来源的藏品、流转事件和权利主张。"""
        events = [dict(e) for e in conn.execute(
            "SELECT id,object_id,event_type,visibility,created_at FROM events WHERE source_id=? ORDER BY id",
            (source_id,)).fetchall()]
        object_ids = sorted({e["object_id"] for e in events})
        objects, claims = [], []
        if object_ids:
            marks = ",".join("?" * len(object_ids))
            objects = [dict(o) for o in conn.execute(
                f"SELECT id,inventory_no,title,version FROM objects WHERE id IN ({marks}) ORDER BY id",
                object_ids).fetchall()]
            claims = [dict(c) for c in conn.execute(
                f"SELECT id,object_id,claimant_id,claimed_by,status,created_at FROM claims WHERE object_id IN ({marks}) ORDER BY id",
                object_ids).fetchall()]
        return {"objects": objects, "events": events, "claims": claims}

    @staticmethod
    def _scope_token(scope):
        key = {k: [x["id"] for x in scope[k]] for k in ("objects", "events", "claims")}
        return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _new_entries(old, new):
        seen = {x["id"] for x in old}
        return [x for x in new if x["id"] not in seen]

    def _update_row(self, conn, update_id):
        row = conn.execute("SELECT * FROM source_updates WHERE id=?", (update_id,)).fetchone()
        if not row:
            raise BusinessError("来源更新请求不存在", 404, "not_found")
        return row

    def _update_progress(self, conn, update_id):
        items = [dict(r) for r in conn.execute(
            "SELECT object_id,status,processed_at FROM source_update_items WHERE update_id=? ORDER BY object_id",
            (update_id,)).fetchall()]
        done = sum(1 for i in items if i["status"] == "done")
        return {"total": len(items), "done": done, "pending": len(items) - done, "items": items}

    def create_source_update(self, user_id, source_id, kind="withdrawal", reason="", correction=None):
        kind = (kind or "withdrawal").strip()
        if kind not in SOURCE_UPDATE_KINDS:
            raise BusinessError("kind 必须是 withdrawal 或 correction", 422, "invalid_kind")
        if not str(reason).strip():
            raise BusinessError("必须填写撤回或更正原因", 422, "reason_required")
        correction = correction or {}
        if kind == "correction":
            correction = {k: str(v).strip() for k, v in correction.items()
                          if k in {"name", "reference"} and str(v).strip()}
            if not correction:
                raise BusinessError("更正必须提供新的名称或引用", 422, "invalid_correction")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            source = self._source(conn, source_id)
            if source["status"] == "withdrawn":
                raise BusinessError("来源已撤回，不能再次发起", 409, "source_withdrawn")
            scope = self._impact_scope(conn, source_id)
            token = self._scope_token(scope)
            cur = conn.execute(
                """INSERT INTO source_updates(source_id,kind,reason,status,scope,scope_token,correction,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (source_id, kind, str(reason).strip(), "pending_confirmation",
                 json.dumps(scope, ensure_ascii=False, sort_keys=True), token,
                 json.dumps(correction, ensure_ascii=False, sort_keys=True) if correction else None,
                 user_id, now()))
            self._audit(conn, None, user_id, "source_update.create",
                        {"update_id": cur.lastrowid, "source_id": source_id, "kind": kind})
            return {"id": cur.lastrowid, "source_id": source_id, "kind": kind,
                    "status": "pending_confirmation", "impact": scope, "scope_token": token}

    def confirm_source_update(self, user_id, update_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            conn.execute("BEGIN IMMEDIATE")
            upd = self._update_row(conn, update_id)
            if upd["status"] == "applied":
                raise BusinessError("该请求已生效，重复提交不会再次写入", 409, "update_already_applied",
                                    {"impact": self._impact_scope(conn, upd["source_id"])})
            if upd["status"] == "applying":
                raise BusinessError("批量写入中断，请使用重试接口续做", 409, "update_in_progress",
                                    {"progress": self._update_progress(conn, update_id)})
            blocker = conn.execute(
                """SELECT id FROM source_updates
                   WHERE source_id=? AND id<>? AND status IN ('applying','applied')
                   ORDER BY id DESC LIMIT 1""", (upd["source_id"], update_id)).fetchone()
            if blocker:
                # 先到者已生效，后到者拿到最新影响范围重新核对。
                raise BusinessError("该来源已有在先的撤回或更正生效，请基于最新影响范围重新核对", 409,
                                    "source_update_conflict",
                                    {"blocking_update_id": blocker["id"],
                                     "impact": self._impact_scope(conn, upd["source_id"])})
            scope = self._impact_scope(conn, upd["source_id"])
            token = self._scope_token(scope)
            if token != upd["scope_token"]:
                old = json.loads(upd["scope"])
                conn.execute("UPDATE source_updates SET status='pending_retry',scope=?,scope_token=? WHERE id=?",
                             (json.dumps(scope, ensure_ascii=False, sort_keys=True), token, update_id))
                conn.commit()
                raise BusinessError("确认前出现新增引用，请求已停在待重试", 409, "scope_changed",
                                    {"status": "pending_retry", "impact": scope,
                                     "new_objects": self._new_entries(old["objects"], scope["objects"]),
                                     "new_events": self._new_entries(old["events"], scope["events"]),
                                     "new_claims": self._new_entries(old["claims"], scope["claims"])})
            conn.execute("UPDATE source_updates SET status='applying' WHERE id=?", (update_id,))
            for obj in scope["objects"]:
                conn.execute("INSERT OR IGNORE INTO source_update_items(update_id,object_id) VALUES(?,?)",
                             (update_id, obj["id"]))
            conn.commit()
        self._run_update_items(update_id, user_id)
        return self._finish_update(user_id, update_id)

    def refresh_source_update(self, user_id, update_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            conn.execute("BEGIN IMMEDIATE")
            upd = self._update_row(conn, update_id)
            if upd["status"] not in ("pending_confirmation", "pending_retry"):
                raise BusinessError("当前状态不能重新核对", 409, "invalid_refresh")
            scope = self._impact_scope(conn, upd["source_id"])
            token = self._scope_token(scope)
            conn.execute(
                "UPDATE source_updates SET status='pending_confirmation',scope=?,scope_token=? WHERE id=?",
                (json.dumps(scope, ensure_ascii=False, sort_keys=True), token, update_id))
            conn.commit()
            return {"id": update_id, "status": "pending_confirmation", "impact": scope, "scope_token": token}

    def retry_source_update(self, user_id, update_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            conn.execute("BEGIN IMMEDIATE")
            upd = self._update_row(conn, update_id)
            if upd["status"] == "applied":
                raise BusinessError("该请求已生效，无需重试", 409, "update_already_applied",
                                    {"impact": self._impact_scope(conn, upd["source_id"])})
            if upd["status"] != "applying":
                raise BusinessError("只有批量写入中断的请求才能重试", 409, "invalid_retry")
            conn.commit()
        self._run_update_items(update_id, user_id)
        return self._finish_update(user_id, update_id)

    def _apply_item(self, conn, update, object_id, actor):
        """单个藏品的写入：未完成主张失效重算、版本快照与审计。失败整体回滚本藏品。"""
        obj = self._object(conn, object_id)
        claims = conn.execute(
            f"SELECT * FROM claims WHERE object_id=? AND status IN ({','.join('?' * len(UNFINISHED_CLAIM_STATUS))}) ORDER BY id",
            (object_id, *UNFINISHED_CLAIM_STATUS)).fetchall()
        kind_label = "撤回" if update["kind"] == "withdrawal" else "更正"
        note = f"来源{kind_label}（{update['reason']}），主张失效并重算"
        invalidated = []
        for c in claims:
            conn.execute("UPDATE claims SET status='invalidated',updated_at=? WHERE id=? AND status=?",
                         (now(), c["id"], c["status"]))
            conn.execute(
                "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                (c["id"], actor, c["status"], "invalidated", note, now()))
            invalidated.append(c["id"])
        new_version = obj["version"] + 1
        conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
        self._snapshot(conn, object_id, actor)
        self._audit(conn, object_id, actor, "source_update.apply",
                    {"update_id": update["id"], "source_id": update["source_id"],
                     "invalidated_claims": invalidated, "version": new_version})
        conn.execute("UPDATE source_update_items SET status='done',processed_at=? WHERE update_id=? AND object_id=?",
                     (now(), update["id"], object_id))

    def _run_update_items(self, update_id, actor):
        """逐藏品独立事务写入；失败保留已完成进度，重试只续做未完成对象。"""
        try:
            while True:
                with self.connect() as conn:
                    item = conn.execute(
                        "SELECT id,object_id FROM source_update_items WHERE update_id=? AND status='pending' ORDER BY object_id LIMIT 1",
                        (update_id,)).fetchone()
                    if not item:
                        return
                    update = dict(self._update_row(conn, update_id))
                with self.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    still = conn.execute("SELECT status FROM source_update_items WHERE id=?", (item["id"],)).fetchone()
                    if still["status"] == "pending":
                        self._apply_item(conn, update, item["object_id"], actor)
                    conn.commit()
        except BusinessError:
            raise
        except Exception as exc:
            with self.connect() as conn:
                progress = self._update_progress(conn, update_id)
            raise BusinessError(f"批量写入失败，进度已保留，可重试续做：{exc}", 500, "apply_failed",
                                {"update_id": update_id, "progress": progress})

    def _finish_update(self, user_id, update_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            upd = self._update_row(conn, update_id)
            progress = self._update_progress(conn, update_id)
            if progress["pending"]:
                conn.commit()
                return {"id": update_id, "status": upd["status"], "progress": progress}
            if upd["status"] != "applied":
                if upd["kind"] == "withdrawal":
                    conn.execute("UPDATE sources SET status='withdrawn' WHERE id=?", (upd["source_id"],))
                else:
                    correction = json.loads(upd["correction"] or "{}")
                    if correction:
                        assignments = ",".join(f"{k}=?" for k in correction)
                        try:
                            conn.execute(f"UPDATE sources SET {assignments},status='corrected' WHERE id=?",
                                         (*correction.values(), upd["source_id"]))
                        except sqlite3.IntegrityError:
                            raise BusinessError("更正后的来源名称和引用与现有记录冲突", 409, "source_exists")
                    else:
                        conn.execute("UPDATE sources SET status='corrected' WHERE id=?", (upd["source_id"],))
                # 生效后重算最新影响范围，供后到者与后续核对使用。
                scope = self._impact_scope(conn, upd["source_id"])
                conn.execute("UPDATE source_updates SET status='applied',applied_at=?,scope=?,scope_token=? WHERE id=?",
                             (now(), json.dumps(scope, ensure_ascii=False, sort_keys=True),
                              self._scope_token(scope), update_id))
                self._audit(conn, None, user_id, "source_update.applied",
                            {"update_id": update_id, "source_id": upd["source_id"], "kind": upd["kind"]})
            result = {"id": update_id, "status": "applied",
                      "progress": self._update_progress(conn, update_id),
                      "impact": self._impact_scope(conn, upd["source_id"])}
            conn.commit()
            return result

    def get_source_update(self, user_id, update_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            upd = self._update_row(conn, update_id)
            if user["role"] == "public":
                raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
            base = {"id": upd["id"], "source_id": upd["source_id"], "kind": upd["kind"],
                    "reason": upd["reason"], "status": upd["status"], "created_by": upd["created_by"],
                    "created_at": upd["created_at"], "applied_at": upd["applied_at"]}
            scope = self._impact_scope(conn, upd["source_id"])
            if user["role"] in ("staff", "reviewer"):
                base["impact"] = scope
                base["progress"] = self._update_progress(conn, update_id)
                base["scope_token"] = upd["scope_token"]
                return base
            # 主张人只能看到与自己主张有关的影响。
            my_claims = [{k: c[k] for k in ("id", "object_id", "claimed_by", "status", "created_at")}
                         for c in scope["claims"] if c["claimant_id"] == user_id]
            object_ids = {c["object_id"] for c in my_claims}
            base["impact"] = {
                "objects": [o for o in scope["objects"] if o["id"] in object_ids],
                "events": [e for e in scope["events"] if e["object_id"] in object_ids and e["visibility"] == "public"],
                "claims": my_claims,
            }
            return base

    def list_source_updates(self, user_id, source_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
            self._source(conn, source_id)
            rows = conn.execute("SELECT * FROM source_updates WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
            items = []
            for r in rows:
                if user["role"] == "claimant":
                    scope = json.loads(r["scope"])
                    if not any(c["claimant_id"] == user_id for c in scope["claims"]):
                        continue
                items.append({"id": r["id"], "source_id": r["source_id"], "kind": r["kind"],
                              "reason": r["reason"], "status": r["status"], "created_by": r["created_by"],
                              "created_at": r["created_at"], "applied_at": r["applied_at"]})
            return items

    def source_impact(self, user_id, source_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._source(conn, source_id)
            return {"source_id": source_id, "impact": self._impact_scope(conn, source_id)}

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference,status FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) == 4 and parts[:2] == ["api", "sources"]:
            source_id = int(parts[2])
            if parts[3] == "updates" and method == "POST":
                d = self._body(); return self._send(201, store.create_source_update(user, source_id, d.get("kind", "withdrawal"), d.get("reason", ""), d.get("correction")))
            if parts[3] == "updates" and method == "GET": return self._send(200, {"items": store.list_source_updates(user, source_id)})
            if parts[3] == "impact" and method == "GET": return self._send(200, store.source_impact(user, source_id))
        if len(parts) >= 3 and parts[:2] == ["api", "source-updates"]:
            update_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_source_update(user, update_id))
            if len(parts) == 4 and parts[3] == "confirm" and method == "POST": return self._send(200, store.confirm_source_update(user, update_id))
            if len(parts) == 4 and parts[3] == "refresh" and method == "POST": return self._send(200, store.refresh_source_update(user, update_id))
            if len(parts) == 4 and parts[3] == "retry" and method == "POST": return self._send(200, store.retry_source_update(user, update_id))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.details is not None: error["details"] = exc.details
            self._send(exc.status, {"error": error})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()

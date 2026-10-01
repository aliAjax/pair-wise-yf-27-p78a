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
}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


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
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retracted')),
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
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    impact_status TEXT NOT NULL DEFAULT 'normal'
                        CHECK(impact_status IN ('normal','invalid')),
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
                CREATE TABLE IF NOT EXISTS source_retractions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','pending_retry','confirmed')),
                    impact_scope TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    confirmed_by TEXT REFERENCES users(id),
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS retraction_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    retraction_id INTEGER NOT NULL REFERENCES source_retractions(id),
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','done','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(retraction_id, object_id)
                );
                CREATE TABLE IF NOT EXISTS source_updates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    changes TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_source_retractions_active
                    ON source_retractions(source_id)
                    WHERE status IN ('pending','pending_retry');
                """
            )
            for ddl in (
                "ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retracted'))",
                "ALTER TABLE claims ADD COLUMN impact_status TEXT NOT NULL DEFAULT 'normal' CHECK(impact_status IN ('normal','invalid'))",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("claimant2", "第二主张人", "claimant"),
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

    def _compute_impact(self, conn, source_id):
        """列出引用该来源的藏品、流转事件，以及依赖这些藏品的权利主张。"""
        events = conn.execute(
            "SELECT id,object_id FROM events WHERE source_id=? ORDER BY id", (source_id,)
        ).fetchall()
        event_ids = [e["id"] for e in events]
        object_ids = sorted({e["object_id"] for e in events})
        claim_ids = []
        if object_ids:
            placeholders = ",".join("?" * len(object_ids))
            claim_ids = [c["id"] for c in conn.execute(
                f"SELECT id FROM claims WHERE object_id IN ({placeholders}) ORDER BY id", object_ids
            ).fetchall()]
        return {"object_ids": object_ids, "event_ids": event_ids, "claim_ids": claim_ids}

    def _absorb_new_impact(self, conn, retraction, impact):
        """把新进入影响范围的藏品补入撤回明细，返回范围是否有变化。"""
        stored = json.loads(retraction["impact_scope"])
        if stored == impact:
            return False
        conn.execute(
            "UPDATE source_retractions SET impact_scope=? WHERE id=?",
            (json.dumps(impact, ensure_ascii=False, sort_keys=True), retraction["id"]),
        )
        existing = {r["object_id"] for r in conn.execute(
            "SELECT object_id FROM retraction_items WHERE retraction_id=?", (retraction["id"],)
        ).fetchall()}
        for oid in impact["object_ids"]:
            if oid not in existing:
                conn.execute(
                    "INSERT INTO retraction_items(retraction_id,object_id,status,attempts,updated_at) VALUES(?,?,?,?,?)",
                    (retraction["id"], oid, "pending", 0, now()),
                )
        return True

    def _claimant_impact(self, conn, impact, user_id):
        """主张人只能看到与自己主张有关的影响。"""
        rows = conn.execute(
            "SELECT id,object_id FROM claims WHERE claimant_id=?", (user_id,)
        ).fetchall()
        own = {r["id"]: r["object_id"] for r in rows}
        claim_ids = [cid for cid in impact["claim_ids"] if cid in own]
        object_ids = sorted({own[cid] for cid in claim_ids})
        event_ids = []
        if impact["event_ids"] and object_ids:
            ph_e = ",".join("?" * len(impact["event_ids"]))
            ph_o = ",".join("?" * len(object_ids))
            event_ids = [e["id"] for e in conn.execute(
                f"SELECT id FROM events WHERE id IN ({ph_e}) AND object_id IN ({ph_o})",
                [*impact["event_ids"], *object_ids],
            ).fetchall()]
        return {"object_ids": object_ids, "event_ids": event_ids, "claim_ids": claim_ids}

    def create_retraction(self, user_id, source_id, reason):
        """工作人员选定来源、填写原因，发起撤回并先拿到影响范围。"""
        reason = reason.strip()
        if not reason:
            raise BusinessError("撤回原因不能为空", 422, "invalid_reason")
        with self._lock:
            with self.connect() as conn:
                self._user(conn, user_id, {"staff"})
                source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if not source:
                    raise BusinessError("来源不存在", 404, "source_not_found")
                if source["status"] == "retracted":
                    raise BusinessError("来源已撤回，不能重复发起", 409, "source_already_retracted")
                impact = self._compute_impact(conn, source_id)
                existing = conn.execute(
                    "SELECT * FROM source_retractions WHERE source_id=? AND status IN ('pending','pending_retry') ORDER BY id DESC LIMIT 1",
                    (source_id,),
                ).fetchone()
                if existing:
                    # 先到者生效：后到的提交不新建撤回，直接拿到最新影响范围。
                    self._absorb_new_impact(conn, existing, impact)
                    return {"retraction_id": existing["id"], "created": False, "status": existing["status"],
                            "impact": impact, "reason": existing["reason"]}
                try:
                    cur = conn.execute(
                        """INSERT INTO source_retractions(source_id,reason,status,impact_scope,created_by,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (source_id, reason, "pending", json.dumps(impact, ensure_ascii=False, sort_keys=True), user_id, now()),
                    )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT * FROM source_retractions WHERE source_id=? AND status IN ('pending','pending_retry') ORDER BY id DESC LIMIT 1",
                        (source_id,),
                    ).fetchone()
                    if existing:
                        self._absorb_new_impact(conn, existing, impact)
                        return {"retraction_id": existing["id"], "created": False, "status": existing["status"],
                                "impact": impact, "reason": existing["reason"]}
                    raise BusinessError("来源已撤回", 409, "source_already_retracted")
                rid = cur.lastrowid
                for oid in impact["object_ids"]:
                    conn.execute(
                        "INSERT INTO retraction_items(retraction_id,object_id,status,attempts,updated_at) VALUES(?,?,?,?,?)",
                        (rid, oid, "pending", 0, now()),
                    )
                for oid in impact["object_ids"]:
                    self._audit(conn, oid, user_id, "retraction.create",
                                {"retraction_id": rid, "source_id": source_id, "reason": reason})
                return {"retraction_id": rid, "created": True, "status": "pending",
                        "impact": impact, "reason": reason}

    def _process_retraction_item(self, conn, retraction_id, item_id, object_id, source_id, reason, actor_id):
        row = conn.execute("SELECT status FROM retraction_items WHERE id=?", (item_id,)).fetchone()
        if not row or row["status"] == "done":
            return  # 重复提交不写第二遍审计。
        obj = self._object(conn, object_id)
        next_version = obj["version"] + 1
        conn.execute("UPDATE objects SET version=?, updated_at=? WHERE id=?", (next_version, now(), object_id))
        self._snapshot(conn, object_id, actor_id)
        self._audit(conn, object_id, actor_id, "retraction.confirm",
                    {"retraction_id": retraction_id, "source_id": source_id,
                     "reason": reason, "version": next_version})
        conn.execute(
            "UPDATE retraction_items SET status='done', attempts=attempts+1, last_error=NULL, updated_at=? WHERE id=?",
            (now(), item_id),
        )

    def confirm_retraction(self, user_id, retraction_id, fail_after=None):
        """确认撤回：批量写入影响对象，失败保留进度，重试只续做未完成对象。"""
        with self._lock:
            with self.connect() as conn:
                self._user(conn, user_id, {"staff"})
                r = conn.execute("SELECT * FROM source_retractions WHERE id=?", (retraction_id,)).fetchone()
                if not r:
                    raise BusinessError("撤回请求不存在", 404, "retraction_not_found")
                source = conn.execute("SELECT * FROM sources WHERE id=?", (r["source_id"],)).fetchone()
                if r["status"] == "confirmed" or source["status"] == "retracted":
                    # 先到者已生效：后到者拿到最新影响范围，不重复写审计。
                    impact = self._compute_impact(conn, r["source_id"])
                    return {"retraction_id": retraction_id, "status": "confirmed",
                            "already_confirmed": True, "impact": impact, "processed": 0}
                impact = self._compute_impact(conn, r["source_id"])
                stored = json.loads(r["impact_scope"])
                new_object_ids = sorted(set(impact["object_ids"]) - set(stored["object_ids"]))
                self._absorb_new_impact(conn, r, impact)
                if new_object_ids:
                    # 确认前新增引用：停在待重试，把新对象纳入影响范围。
                    conn.execute("UPDATE source_retractions SET status='pending_retry' WHERE id=?", (retraction_id,))
                    return {"retraction_id": retraction_id, "status": "pending_retry",
                            "new_object_ids": new_object_ids, "impact": impact}
                item_rows = conn.execute(
                    "SELECT id,object_id FROM retraction_items WHERE retraction_id=? AND status!='done' ORDER BY object_id",
                    (retraction_id,),
                ).fetchall()
            processed = 0
            for item in item_rows:
                if fail_after is not None and processed >= fail_after:
                    raise BusinessError("批量写入失败，进度已保留，可重试续做", 500, "batch_failed")
                with self.connect() as conn:
                    self._process_retraction_item(
                        conn, retraction_id, item["id"], item["object_id"],
                        r["source_id"], r["reason"], user_id,
                    )
                processed += 1
            with self.connect() as conn:
                conn.execute("UPDATE sources SET status='retracted' WHERE id=?", (r["source_id"],))
                conn.execute(
                    "UPDATE source_retractions SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                    (user_id, now(), retraction_id),
                )
            return {"retraction_id": retraction_id, "status": "confirmed",
                    "already_confirmed": False, "impact": impact, "processed": processed}

    def get_retraction(self, user_id, retraction_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            r = conn.execute("SELECT * FROM source_retractions WHERE id=?", (retraction_id,)).fetchone()
            if not r:
                raise BusinessError("撤回请求不存在", 404, "retraction_not_found")
            if user["role"] == "public":
                raise BusinessError("公众无权查看撤回详情", 403, "forbidden")
            impact = json.loads(r["impact_scope"])
            items = [dict(i) for i in conn.execute(
                "SELECT id,object_id,status,attempts,updated_at FROM retraction_items WHERE retraction_id=? ORDER BY object_id",
                (retraction_id,),
            ).fetchall()]
            if user["role"] == "claimant":
                impact = self._claimant_impact(conn, impact, user_id)
                items = [i for i in items if i["object_id"] in set(impact["object_ids"])]
            return {
                "id": r["id"], "source_id": r["source_id"], "reason": r["reason"],
                "status": r["status"], "created_at": r["created_at"],
                "confirmed_at": r["confirmed_at"], "impact": impact, "items": items,
            }

    def list_retractions(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                raise BusinessError("公众无权查看撤回列表", 403, "forbidden")
            rows = conn.execute("SELECT * FROM source_retractions ORDER BY id DESC").fetchall()
            out = []
            for r in rows:
                impact = json.loads(r["impact_scope"])
                if user["role"] == "claimant":
                    impact = self._claimant_impact(conn, impact, user_id)
                    if not impact["claim_ids"]:
                        continue  # 不影响该主张人的撤回不列出。
                out.append({
                    "id": r["id"], "source_id": r["source_id"], "reason": r["reason"],
                    "status": r["status"], "created_at": r["created_at"],
                    "confirmed_at": r["confirmed_at"], "impact": impact,
                })
            return out

    def update_source(self, user_id, source_id, changes):
        """来源更正：依赖它的未完成主张失效并重算，已完成返还和快照保留。"""
        allowed = {"name", "source_type", "reference"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self._lock:
            with self.connect() as conn:
                self._user(conn, user_id, {"staff"})
                source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if not source:
                    raise BusinessError("来源不存在", 404, "source_not_found")
                assignments = ",".join(f"{k}=?" for k in clean)
                conn.execute(f"UPDATE sources SET {assignments} WHERE id=?", (*clean.values(), source_id))
                conn.execute(
                    "INSERT INTO source_updates(source_id,changes,created_by,created_at) VALUES(?,?,?,?)",
                    (source_id, json.dumps(clean, ensure_ascii=False, sort_keys=True), user_id, now()),
                )
                impact = self._compute_impact(conn, source_id)
                invalidated = []
                affected_objects = set()
                if impact["claim_ids"]:
                    ph = ",".join("?" * len(impact["claim_ids"]))
                    unfinished = conn.execute(
                        f"SELECT id,object_id,status FROM claims WHERE id IN ({ph}) "
                        "AND status IN ('submitted','under_review','negotiating')",
                        impact["claim_ids"],
                    ).fetchall()
                    for c in unfinished:
                        conn.execute("UPDATE claims SET impact_status='invalid', updated_at=? WHERE id=?",
                                     (now(), c["id"]))
                        invalidated.append({"claim_id": c["id"], "object_id": c["object_id"], "old_status": c["status"]})
                        affected_objects.add(c["object_id"])
                for oid in sorted(affected_objects):
                    obj = self._object(conn, oid)
                    next_version = obj["version"] + 1
                    conn.execute("UPDATE objects SET version=?, updated_at=? WHERE id=?", (next_version, now(), oid))
                    self._snapshot(conn, oid, user_id)
                    conn.execute(
                        "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                        (oid, user_id, "source.update", json.dumps({
                            "source_id": source_id, "changes": clean,
                            "invalidated_claim_ids": [c["claim_id"] for c in invalidated if c["object_id"] == oid],
                            "version": next_version,
                        }, ensure_ascii=False, sort_keys=True), now()),
                    )
                return {"source_id": source_id, "changes": clean,
                        "invalidated_claims": invalidated,
                        "affected_objects": sorted(affected_objects)}

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
                    "SELECT id,claimed_by,desired_outcome,status,impact_status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
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
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and parts[3] == "retractions" and method == "POST":
            d = self._body(); result = store.create_retraction(user, int(parts[2]), d.get("reason", ""))
            return self._send(201 if result.get("created") else 200, result)
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and parts[3] == "update" and method == "POST":
            d = self._body(); return self._send(200, store.update_source(user, int(parts[2]), d.get("changes", {})))
        if parts == ["api", "retractions"] and method == "GET":
            return self._send(200, {"items": store.list_retractions(user)})
        if len(parts) == 3 and parts[:2] == ["api", "retractions"] and method == "GET":
            return self._send(200, store.get_retraction(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "retractions"] and parts[3] == "confirm" and method == "POST":
            d = self._body(); return self._send(200, store.confirm_retraction(user, int(parts[2]), d.get("fail_after")))
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
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
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

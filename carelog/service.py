"""洗护领域服务：开始前核对、事件状态机、用品消耗、异常复核与通知。

所有写操作都在单个 SQLite 事务内完成；幂等依赖 UNIQUE 约束兜底。
系统只记录与留痕，不做医学判断。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from typing import Any

# 事件类型与允许发生时的会话状态
TERMINAL_STATUSES = ("ended", "aborted")
EVENT_TYPES = ("start", "pause", "resume", "product_change", "observation", "end")


class DomainError(Exception):
    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


# ---------------------------------------------------------------- 基础工具

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def _payload(raw: str) -> dict:
    return json.loads(raw or "{}")


def _allowed_product_ids(scope_json: str | None) -> set[int] | None:
    """None 表示全部允许；否则为白名单集合。"""
    if scope_json is None:
        return None
    scope = json.loads(scope_json)
    allowed = scope.get("product_ids")
    return None if allowed is None else set(allowed)


# ---------------------------------------------------------------- 主数据查询

def get_baby(db, baby_id: int) -> dict | None:
    return _row(db.execute("SELECT * FROM babies WHERE id=?", (baby_id,)).fetchone())


def get_staff(db, staff_id: int) -> dict | None:
    return _row(db.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone())


def get_product(db, product_id: int) -> dict | None:
    return _row(db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())


def latest_authorization(db, baby_id: int) -> dict | None:
    return _row(db.execute(
        "SELECT * FROM authorizations WHERE baby_id=? ORDER BY version DESC LIMIT 1",
        (baby_id,),
    ).fetchone())


def active_notes(db, baby_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT * FROM special_notes WHERE baby_id=? AND active=1 ORDER BY id",
        (baby_id,),
    ).fetchall()]


# ---------------------------------------------------------------- 授权管理

def create_authorization(db, baby_id: int, granted_by: str,
                         scope: dict | None = None) -> dict:
    if get_baby(db, baby_id) is None:
        raise DomainError("baby_not_found", "婴儿不存在", 404)
    prev = latest_authorization(db, baby_id)
    if prev is not None and prev["status"] == "active":
        raise DomainError("authorization_exists",
                          "已有有效授权，请使用缩小范围接口产生新版本", 409)
    version = (prev["version"] + 1) if prev else 1
    cur = db.execute(
        """INSERT INTO authorizations (baby_id, version, supersedes_id, granted_by, scope_json)
           VALUES (?,?,?,?,?)""",
        (baby_id, version, prev["id"] if prev else None, granted_by,
         json.dumps(scope, ensure_ascii=False) if scope is not None else None),
    )
    return _row(db.execute("SELECT * FROM authorizations WHERE id=?",
                           (cur.lastrowid,)).fetchone())


def narrow_authorization(db, baby_id: int, granted_by: str, scope: dict) -> dict:
    """家长在操作前缩小授权范围：只能取旧范围的子集，生成新版本。"""
    prev = latest_authorization(db, baby_id)
    if prev is None:
        raise DomainError("authorization_missing", "尚无授权，无法缩小范围", 403)
    if prev["status"] != "active":
        raise DomainError("authorization_withdrawn", "授权已撤回，不能变更", 409)
    live = db.execute(
        "SELECT 1 FROM sessions WHERE baby_id=? AND status IN ('in_progress','paused') "
        "LIMIT 1", (baby_id,)).fetchone()
    if live is not None:
        raise DomainError("session_in_progress",
                          "洗护进行中不能缩小授权范围（只能在操作前变更）", 409)
    new_ids = set(scope.get("product_ids") or [])
    if not new_ids:
        raise DomainError("empty_scope", "缩小后的范围不能为空", 400)
    old_allowed = _allowed_product_ids(prev["scope_json"])
    if old_allowed is not None and not new_ids <= old_allowed:
        raise DomainError("scope_not_subset",
                          "新范围必须是原授权范围的子集（只能缩小）", 400)
    for pid in new_ids:
        if get_product(db, pid) is None:
            raise DomainError("product_not_found", f"用品 {pid} 不存在", 404)
    cur = db.execute(
        """INSERT INTO authorizations (baby_id, version, supersedes_id, granted_by, scope_json)
           VALUES (?,?,?,?,?)""",
        (baby_id, prev["version"] + 1, prev["id"], granted_by,
         json.dumps({"product_ids": sorted(new_ids)}, ensure_ascii=False)),
    )
    db.execute("UPDATE authorizations SET status='superseded' WHERE id=?",
               (prev["id"],))
    return _row(db.execute("SELECT * FROM authorizations WHERE id=?",
                           (cur.lastrowid,)).fetchone())


def withdraw_authorization(db, auth_id: int) -> dict:
    auth = _row(db.execute("SELECT * FROM authorizations WHERE id=?",
                           (auth_id,)).fetchone())
    if auth is None:
        raise DomainError("authorization_not_found", "授权不存在", 404)
    if auth["status"] in ("withdrawn", "superseded"):
        return auth
    db.execute(
        "UPDATE authorizations SET status='withdrawn', withdrawn_at=? WHERE id=?",
        (now(), auth_id),
    )
    return _row(db.execute("SELECT * FROM authorizations WHERE id=?",
                           (auth_id,)).fetchone())


# ---------------------------------------------------------------- 开始前核对

def _verify_prerequisites(db, *, baby_id: int, wristband_code: str,
                          staff_id: int, product_ids: list[int],
                          note_acknowledged: bool) -> tuple[dict, dict, list[dict]]:
    baby = get_baby(db, baby_id)
    if baby is None:
        raise DomainError("baby_not_found", "婴儿不存在", 404)
    # 1) 身份核对：腕带扫码必须与档案一致
    if baby["wristband_code"] != wristband_code:
        raise DomainError("identity_mismatch",
                          "腕带扫码与婴儿身份不一致，不得开始", 409)

    # 2) 护理人员资质：在册、未停用、未过期
    staff = get_staff(db, staff_id)
    if staff is None:
        raise DomainError("staff_not_found", "护理人员不存在", 404)
    if not staff["active"]:
        raise DomainError("staff_inactive", "护理人员资质已停用", 403)
    if staff["valid_until"] < date.today().isoformat():
        raise DomainError("staff_qualification_expired",
                          "护理人员资质已过期，不得开始", 403)

    # 3) 家长授权：必须存在系统内显式有效的最新版本
    auth = latest_authorization(db, baby_id)
    if auth is None or auth["status"] != "active":
        raise DomainError("authorization_missing",
                          "缺少有效的家长授权（口头顾虑不能视为授权），不得开始", 403)

    # 4) 用品：存在、中心未停用、在授权范围内
    allowed = _allowed_product_ids(auth["scope_json"])
    seen = set()
    for pid in product_ids:
        if pid in seen:
            continue
        seen.add(pid)
        product = get_product(db, pid)
        if product is None:
            raise DomainError("product_not_found", f"用品 {pid} 不存在", 404)
        if product["status"] != "active":
            raise DomainError("product_deactivated",
                              f"用品 {product['name']}（批次 {product['batch_no']}）"
                              "已被中心停用，不得开始", 409)
        if allowed is not None and pid not in allowed:
            raise DomainError("product_not_authorized",
                              f"用品 {product['name']} 不在家长授权范围内，不得开始", 403)

    # 5) 特殊注意事项必须现场确认已知悉
    notes = active_notes(db, baby_id)
    if notes and not note_acknowledged:
        raise DomainError("special_notes_unacknowledged",
                          f"存在 {len(notes)} 条特殊注意事项，须现场确认知悉后才能开始",
                          403)

    return baby, staff, [auth], notes


def start_session(db, *, idempotency_key: str, baby_id: int, wristband_code: str,
                  staff_id: int, action: str, note_acknowledged: bool,
                  scans: list[dict]) -> tuple[dict, bool]:
    """返回 (session, created)。重传相同幂等键返回原记录，不重复消耗用品。"""
    if not idempotency_key:
        raise DomainError("idempotency_key_required", "缺少幂等键", 400)

    existing = _row(db.execute("SELECT * FROM sessions WHERE idempotency_key=?",
                               (idempotency_key,)).fetchone())
    if existing is not None:
        return existing, False

    product_ids = [s["product_id"] for s in scans]
    baby, _staff, (auth,), _notes = _verify_prerequisites(
        db, baby_id=baby_id, wristband_code=wristband_code, staff_id=staff_id,
        product_ids=product_ids, note_acknowledged=note_acknowledged,
    )

    snapshot = {"scope_json": json.loads(auth["scope_json"])
                if auth["scope_json"] is not None else None}
    cur = db.execute(
        """INSERT INTO sessions
           (baby_id, staff_id, auth_id, idempotency_key, auth_version,
            auth_snapshot_json, action, status)
           VALUES (?,?,?,?,?,?,?, 'in_progress')""",
        (baby_id, staff_id, auth["id"], idempotency_key, auth["version"],
         json.dumps(snapshot, ensure_ascii=False), action),
    )
    session_id = cur.lastrowid
    _append_event(db, session_id, "start",
                  {"action": action, "auth_version": auth["version"],
                   "initial_product_ids": product_ids})
    # 原子消耗首批扫码用品；scan_token 全局唯一，重传不会二次消耗
    for s in scans:
        _consume(db, session_id, None, s["scan_token"], s["product_id"])
    return _row(db.execute("SELECT * FROM sessions WHERE id=?",
                           (session_id,)).fetchone()), True


# ---------------------------------------------------------------- 事件与消耗

def get_session(db, session_id: int) -> dict | None:
    return _row(db.execute("SELECT * FROM sessions WHERE id=?",
                           (session_id,)).fetchone())


def _next_seq(db, session_id: int) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM events WHERE session_id=?",
                     (session_id,)).fetchone()
    return row["s"]


def _append_event(db, session_id: int, event_type: str, payload: dict,
                  client_event_id: str | None = None) -> dict:
    cur = db.execute(
        """INSERT INTO events (session_id, seq, type, client_event_id, payload_json)
           VALUES (?,?,?,?,?)""",
        (session_id, _next_seq(db, session_id), event_type, client_event_id,
         json.dumps(payload, ensure_ascii=False, default=str)),
    )
    return _row(db.execute("SELECT * FROM events WHERE id=?",
                           (cur.lastrowid,)).fetchone())


def _consume(db, session_id: int, event_id: int | None,
             scan_token: str, product_id: int) -> dict:
    product = get_product(db, product_id)
    if product is None:
        raise DomainError("product_not_found", f"用品 {product_id} 不存在", 404)
    if product["status"] != "active":
        raise DomainError("product_deactivated",
                          f"用品 {product['name']}（批次 {product['batch_no']}）"
                          "已被中心停用", 409)
    old = db.execute("SELECT id, session_id FROM product_uses WHERE scan_token=?",
                     (scan_token,)).fetchone()
    if old is not None:
        if old["session_id"] == session_id:
            raise DomainError("duplicate_scan",
                              "该用品扫码已在本次洗护中消耗，不能重复扫描", 409)
        raise DomainError("scan_token_consumed",
                          "该用品扫码已在其他操作中消耗，不能重复使用", 409)
    cur = db.execute(
        """INSERT INTO product_uses (session_id, product_id, batch_no, scan_token, event_id)
           VALUES (?,?,?,?,?)""",
        (session_id, product_id, product["batch_no"], scan_token, event_id),
    )
    return _row(db.execute("SELECT * FROM product_uses WHERE id=?",
                           (cur.lastrowid,)).fetchone())


def append_event(db, session_id: int, *, event_type: str, client_event_id: str | None,
                 payload: dict) -> dict:
    session = get_session(db, session_id)
    if session is None:
        raise DomainError("session_not_found", "洗护记录不存在", 404)

    # 事件级幂等：同一 client_event_id 重放返回原事件
    if client_event_id:
        dup = db.execute(
            "SELECT * FROM events WHERE session_id=? AND client_event_id=?",
            (session_id, client_event_id),
        ).fetchone()
        if dup is not None:
            return dict(dup)

    if session["status"] in TERMINAL_STATUSES:
        raise DomainError("session_closed",
                          f"会话已{('终止' if session['status']=='aborted' else '结束')}，"
                          "不能再记录事件", 409)

    auth = _row(db.execute("SELECT * FROM authorizations WHERE id=?",
                           (session["auth_id"],)).fetchone())
    # 授权在操作中途被撤回：普通事件一律拒绝，需立即终止复核
    if auth["status"] == "withdrawn":
        raise DomainError("authorization_withdrawn",
                          "家长已在操作中途撤回授权，请立即终止并启动复核", 409)

    status = session["status"]
    if event_type == "pause":
        if status != "in_progress":
            raise DomainError("invalid_transition", "仅进行中可以暂停", 409)
        db.execute("UPDATE sessions SET status='paused' WHERE id=?", (session_id,))
    elif event_type == "resume":
        if status != "paused":
            raise DomainError("invalid_transition", "仅暂停后可以恢复", 409)
        db.execute("UPDATE sessions SET status='in_progress' WHERE id=?", (session_id,))
    elif event_type == "end":
        if status not in ("in_progress", "paused"):
            raise DomainError("invalid_transition", "当前状态不能结束", 409)
        event = _append_event(db, session_id, "end",
                              {"reason": payload.get("reason", "completed")},
                              client_event_id)
        db.execute(
            "UPDATE sessions SET status='ended', end_reason=?, ended_at=? WHERE id=?",
            (payload.get("reason", "completed"), now(), session_id))
        return event
    elif event_type == "product_change":
        if status != "in_progress":
            raise DomainError("invalid_transition", "仅进行中可以更换用品", 409)
        return _do_product_change(db, session, payload, client_event_id)
    elif event_type == "observation":
        event = _append_event(db, session_id, "observation", payload, client_event_id)
        # 出现不适迹象：立即终止 + 复核 + 通知（系统不做医学判断，只如实留痕）
        if payload.get("discomfort"):
            _abort_locked(db, session, event,
                          reason="discomfort_signs",
                          notify_recipient=payload.get("guardian_contact", "家长"))
        return event
    else:
        raise DomainError("invalid_event_type",
                          f"事件类型 {event_type} 不允许通过此接口写入", 400)

    return _append_event(db, session_id, event_type, payload, client_event_id)


def _do_product_change(db, session: dict, payload: dict,
                       client_event_id: str | None) -> dict:
    product_id = payload.get("product_id")
    scan_token = payload.get("scan_token")
    if not product_id or not scan_token:
        raise DomainError("invalid_payload", "换品需要 product_id 与 scan_token", 400)

    snapshot = json.loads(session["auth_snapshot_json"])
    scope = snapshot["scope_json"]
    allowed = None if scope is None else set(scope.get("product_ids") or [])
    if allowed is not None and product_id not in allowed:
        product = get_product(db, product_id)
        name = product["name"] if product else product_id
        raise DomainError("product_not_authorized",
                          f"用品 {name} 不在授权范围内，禁止更换", 403)

    old_use = db.execute("SELECT * FROM product_uses WHERE scan_token=?",
                         (scan_token,)).fetchone()
    if old_use is not None:
        if old_use["session_id"] == session["id"]:
            # 同一次洗护内重复扫码：返回已存在的消耗，不重复生成事件/消耗
            existing = db.execute(
                "SELECT * FROM events WHERE session_id=? AND type='product_change' "
                "AND json_extract(payload_json,'$.scan_token')=?",
                (session["id"], scan_token)).fetchone()
            if existing is not None:
                return dict(existing)
        raise DomainError("scan_token_consumed",
                          "该用品扫码已在其他操作中消耗，不能重复使用", 409)

    event = _append_event(db, session["id"], "product_change", payload,
                          client_event_id)
    _consume(db, session["id"], event["id"], scan_token, product_id)
    return event


# ---------------------------------------------------------------- 终止与复核

def abort_session(db, session_id: int, *, reason: str,
                  signs: list[str] | None = None, note: str | None = None,
                  guardian_contact: str = "家长", client_event_id: str | None = None
                  ) -> dict:
    session = get_session(db, session_id)
    if session is None:
        raise DomainError("session_not_found", "洗护记录不存在", 404)
    if session["status"] in TERMINAL_STATUSES:
        raise DomainError("session_closed", "会话已结束/终止", 409)

    obs_event = None
    if signs or note:
        obs_event = _append_event(db, session_id, "observation",
                                  {"signs": signs or [], "note": note,
                                   "discomfort": reason == "discomfort_signs"},
                                  client_event_id)
    return _abort_locked(db, session, obs_event, reason=reason,
                         notify_recipient=guardian_contact)


def _abort_locked(db, session: dict, observation_event: dict | None, *,
                  reason: str, notify_recipient: str) -> dict:
    session_id = session["id"]
    end_event = _append_event(db, session_id, "end", {"aborted": True, "reason": reason})
    db.execute(
        "UPDATE sessions SET status='aborted', end_reason=?, ended_at=? WHERE id=?",
        (reason, now(), session_id))
    cur = db.execute(
        """INSERT INTO reviews (session_id, reason, observation_event_id)
           VALUES (?,?,?)""",
        (session_id, reason,
         observation_event["id"] if observation_event else None))
    review_id = cur.lastrowid
    _send_notification(db, session_id, review_id, notify_recipient, reason,
                       observation_event)
    return end_event


def _send_notification(db, session_id: int, review_id: int, recipient: str,
                       reason: str, observation_event: dict | None) -> dict:
    # 模拟推送网关：生成并持久化通知回执（真实部署替换为网关调用）
    receipt = {
        "gateway": "mock-push",
        "message_id": f"msg-{uuid.uuid4().hex[:16]}",
        "sent_at": now(),
        "reason": reason,
    }
    cur = db.execute(
        """INSERT INTO notifications (session_id, review_id, recipient, status, receipt_json)
           VALUES (?,?,?,'sent',?)""",
        (session_id, review_id, recipient,
         json.dumps(receipt, ensure_ascii=False)))
    return _row(db.execute("SELECT * FROM notifications WHERE id=?",
                           (cur.lastrowid,)).fetchone())


def confirm_notification(db, notification_id: int) -> dict:
    row = db.execute("SELECT * FROM notifications WHERE id=?",
                     (notification_id,)).fetchone()
    if row is None:
        raise DomainError("notification_not_found", "通知不存在", 404)
    if row["status"] != "confirmed":
        db.execute(
            "UPDATE notifications SET status='confirmed', confirmed_at=? WHERE id=?",
            (now(), notification_id))
    return _row(db.execute("SELECT * FROM notifications WHERE id=?",
                           (notification_id,)).fetchone())


def resolve_review(db, review_id: int, conclusion: str) -> dict:
    row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise DomainError("review_not_found", "复核记录不存在", 404)
    db.execute(
        "UPDATE reviews SET status='resolved', conclusion=?, resolved_at=? WHERE id=?",
        (conclusion, now(), review_id))
    return _row(db.execute("SELECT * FROM reviews WHERE id=?",
                           (review_id,)).fetchone())


# ---------------------------------------------------------------- 事后更正

def add_correction(db, session_id: int, *, original_event_id: int, aspect: str,
                   new_value: str, reason: str, corrected_by: str) -> dict:
    if not reason:
        raise DomainError("reason_required", "事后更正必须注明原因", 400)
    session = get_session(db, session_id)
    if session is None:
        raise DomainError("session_not_found", "洗护记录不存在", 404)
    event = db.execute("SELECT * FROM events WHERE id=? AND session_id=?",
                       (original_event_id, session_id)).fetchone()
    if event is None:
        raise DomainError("event_not_found", "原记录不存在或不属于该次洗护", 404)
    old_value = _payload(event["payload_json"]).get(aspect)
    cur = db.execute(
        """INSERT INTO corrections
           (session_id, original_event_id, aspect, old_value, new_value, reason, corrected_by)
           VALUES (?,?,?,?,?,?,?)""",
        (session_id, original_event_id, aspect,
         json.dumps(old_value, ensure_ascii=False) if not isinstance(old_value, str)
         else old_value,
         new_value, reason, corrected_by))
    return _row(db.execute("SELECT * FROM corrections WHERE id=?",
                           (cur.lastrowid,)).fetchone())


# ---------------------------------------------------------------- 主管追溯

def trace_session(db, session_id: int) -> dict:
    session = get_session(db, session_id)
    if session is None:
        raise DomainError("session_not_found", "洗护记录不存在", 404)

    auth_chain = [dict(r) for r in db.execute(
        """WITH RECURSIVE chain AS (
               SELECT * FROM authorizations WHERE id=?
               UNION ALL
               SELECT a.* FROM authorizations a JOIN chain c ON a.id=c.supersedes_id
           )
           SELECT * FROM chain ORDER BY version""",
        (session["auth_id"],)).fetchall()]
    for a in auth_chain:
        a["scope"] = json.loads(a["scope_json"]) if a["scope_json"] else None
        del a["scope_json"]

    events = []
    for r in db.execute("SELECT * FROM events WHERE session_id=? ORDER BY seq",
                        (session_id,)).fetchall():
        e = dict(r)
        e["payload"] = json.loads(e.pop("payload_json"))
        events.append(e)

    uses = []
    for r in db.execute(
            """SELECT pu.*, p.name AS product_name, p.supplier,
                      json_object('batch_no', p.batch_no, 'status', p.status) AS product_meta
               FROM product_uses pu JOIN products p ON p.id=pu.product_id
               WHERE pu.session_id=? ORDER BY pu.id""", (session_id,)).fetchall():
        u = dict(r)
        u["product_meta"] = json.loads(u["product_meta"])
        uses.append(u)

    reviews = [dict(r) for r in db.execute(
        "SELECT * FROM reviews WHERE session_id=? ORDER BY id", (session_id,))]
    notifications = []
    for r in db.execute("SELECT * FROM notifications WHERE session_id=? ORDER BY id",
                        (session_id,)):
        n = dict(r)
        n["receipt"] = json.loads(n.pop("receipt_json"))
        notifications.append(n)
    corrections = []
    for r in db.execute("SELECT * FROM corrections WHERE session_id=? ORDER BY id",
                        (session_id,)):
        c = dict(r)
        corrections.append(c)

    baby = get_baby(db, session["baby_id"])
    staff = get_staff(db, session["staff_id"])
    snap = json.loads(session["auth_snapshot_json"])
    session_out = dict(session)
    session_out["auth_snapshot"] = snap
    del session_out["auth_snapshot_json"]

    return {
        "session": session_out,
        "baby": {"id": baby["id"], "name": baby["name"],
                 "wristband_code": baby["wristband_code"]},
        "staff": {"id": staff["id"], "name": staff["name"],
                  "qualification_no": staff["qualification_no"],
                  "valid_until": staff["valid_until"]},
        "authorization_chain": auth_chain,
        "special_notes_at_start": active_notes(db, session["baby_id"]),
        "events": events,
        "product_uses": uses,
        "reviews": reviews,
        "notifications": notifications,
        "corrections": corrections,
    }

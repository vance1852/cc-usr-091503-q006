"""Flask 应用：移动端洗护事件记录接口。

接口清单：
  POST /api/authorizations                建立授权
  POST /api/babies/<id>/authorization/narrow   操作前家长缩小授权范围
  POST /api/authorizations/<id>/withdraw       家长撤回授权
  POST /api/sessions                      开始一次洗护（开始前核对，幂等）
  GET  /api/sessions/<id>                 主管全链路追溯
  POST /api/sessions/<id>/events          追加事件（pause/resume/product_change/observation/end，幂等）
  POST /api/sessions/<id>/abort           不适迹象等立即终止并启动复核
  POST /api/notifications/<id>/confirm    家长确认收到通知
  POST /api/reviews/<id>/resolve          主管完成复核
  POST /api/sessions/<id>/corrections     事后更正（只追加，注明原记录）

另提供主数据维护接口（婴儿、人员、用品批次、注意事项）以便测试与现场建档。
"""
from __future__ import annotations

import os
import sqlite3

from flask import Flask, jsonify, request

from . import service
from .db import close_db, get_db, init_db


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = db_path or os.environ.get(
        "CARELOG_DB", str(os.path.join(os.getcwd(), "carelog.db")))
    app.teardown_appcontext(close_db)

    @app.errorhandler(service.DomainError)
    def handle_domain_error(e: service.DomainError):
        return jsonify({"error": e.code, "message": e.message}), e.http

    @app.errorhandler(sqlite3.IntegrityError)
    def handle_integrity(e):
        # 并发重传兜底：唯一约束冲突视为幂等重放
        return jsonify({"error": "integrity_conflict",
                        "message": f"数据冲突（可能为重复提交）: {e}"}), 409

    def run(fn, *args, **kwargs):
        """在单事务内执行领域函数并提交。"""
        db = get_db()
        try:
            result = fn(db, *args, **kwargs)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise

    # ------------------------------------------------------------ 主数据

    @app.post("/api/babies")
    def create_baby():
        d = request.get_json(force=True)
        db = get_db()
        cur = db.execute("INSERT INTO babies (name, wristband_code) VALUES (?,?)",
                         (d["name"], d["wristband_code"]))
        db.commit()
        row = db.execute("SELECT * FROM babies WHERE id=?", (cur.lastrowid,)).fetchone()
        return jsonify(dict(row)), 201

    @app.post("/api/staff")
    def create_staff():
        d = request.get_json(force=True)
        db = get_db()
        cur = db.execute(
            "INSERT INTO staff (name, qualification_no, valid_until, active) "
            "VALUES (?,?,?,?)",
            (d["name"], d["qualification_no"], d["valid_until"],
             1 if d.get("active", True) else 0))
        db.commit()
        return jsonify(dict(db.execute(
            "SELECT * FROM staff WHERE id=?", (cur.lastrowid,)).fetchone())), 201

    @app.post("/api/products")
    def create_product():
        d = request.get_json(force=True)
        db = get_db()
        cur = db.execute(
            "INSERT INTO products (name, batch_no, supplier) VALUES (?,?,?)",
            (d["name"], d["batch_no"], d["supplier"]))
        db.commit()
        return jsonify(dict(db.execute(
            "SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())), 201

    @app.post("/api/products/<int:pid>/deactivate")
    def deactivate_product(pid):
        db = get_db()
        row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            return jsonify({"error": "product_not_found",
                            "message": "用品不存在"}), 404
        db.execute(
            "UPDATE products SET status='deactivated', deactivated_at=datetime('now') "
            "WHERE id=?", (pid,))
        db.commit()
        return jsonify(dict(db.execute(
            "SELECT * FROM products WHERE id=?", (pid,)).fetchone()))

    @app.post("/api/babies/<int:baby_id>/notes")
    def add_note(baby_id):
        d = request.get_json(force=True)
        db = get_db()
        if db.execute("SELECT 1 FROM babies WHERE id=?", (baby_id,)).fetchone() is None:
            return jsonify({"error": "baby_not_found", "message": "婴儿不存在"}), 404
        cur = db.execute(
            "INSERT INTO special_notes (baby_id, content) VALUES (?,?)",
            (baby_id, d["content"]))
        db.commit()
        return jsonify(dict(db.execute(
            "SELECT * FROM special_notes WHERE id=?",
            (cur.lastrowid,)).fetchone())), 201

    # ------------------------------------------------------------ 授权

    @app.post("/api/authorizations")
    def create_authorization():
        d = request.get_json(force=True)
        auth = run(service.create_authorization,
                   baby_id=d["baby_id"], granted_by=d["granted_by"],
                   scope=d.get("scope"))
        return jsonify(_auth_out(auth)), 201

    @app.post("/api/babies/<int:baby_id>/authorization/narrow")
    def narrow_authorization(baby_id):
        d = request.get_json(force=True)
        auth = run(service.narrow_authorization, baby_id=baby_id,
                   granted_by=d["granted_by"], scope=d["scope"])
        return jsonify(_auth_out(auth)), 201

    @app.post("/api/authorizations/<int:auth_id>/withdraw")
    def withdraw_authorization(auth_id):
        auth = run(service.withdraw_authorization, auth_id)
        return jsonify(_auth_out(auth))

    # ------------------------------------------------------------ 洗护会话

    @app.post("/api/sessions")
    def start_session():
        d = request.get_json(force=True)
        session, created = run(
            service.start_session,
            idempotency_key=d.get("idempotency_key", ""),
            baby_id=d["baby_id"], wristband_code=d["wristband_code"],
            staff_id=d["staff_id"], action=d.get("action", "wash"),
            note_acknowledged=bool(d.get("note_acknowledged", False)),
            scans=d.get("scans", []))
        return jsonify({"session": session, "created": created,
                        "idempotent_replay": not created}), 201 if created else 200

    @app.get("/api/sessions/<int:session_id>")
    def trace_session(session_id):
        return jsonify(run(service.trace_session, session_id))

    @app.post("/api/sessions/<int:session_id>/events")
    def append_event(session_id):
        d = request.get_json(force=True)
        event_type = d.get("type")
        if event_type not in service.EVENT_TYPES:
            return jsonify({"error": "invalid_event_type",
                            "message": f"不支持的事件类型: {event_type}"}), 400
        if event_type == "start":
            return jsonify({"error": "invalid_event_type",
                            "message": "开始事件由 POST /api/sessions 产生"}), 400
        event = run(service.append_event, session_id, event_type=event_type,
                    client_event_id=d.get("client_event_id"),
                    payload=d.get("payload", {}))
        return jsonify(event), 201

    @app.post("/api/sessions/<int:session_id>/abort")
    def abort_session(session_id):
        d = request.get_json(force=True)
        event = run(service.abort_session, session_id,
                    reason=d.get("reason", "aborted"),
                    signs=d.get("signs"), note=d.get("note"),
                    guardian_contact=d.get("guardian_contact", "家长"),
                    client_event_id=d.get("client_event_id"))
        return jsonify({"end_event": event, "aborted": True}), 201

    # ------------------------------------------------------------ 复核 / 通知 / 更正

    @app.post("/api/notifications/<int:notification_id>/confirm")
    def confirm_notification(notification_id):
        return jsonify(run(service.confirm_notification, notification_id))

    @app.post("/api/reviews/<int:review_id>/resolve")
    def resolve_review(review_id):
        d = request.get_json(force=True)
        return jsonify(run(service.resolve_review, review_id, d["conclusion"]))

    @app.post("/api/sessions/<int:session_id>/corrections")
    def add_correction(session_id):
        d = request.get_json(force=True)
        corr = run(service.add_correction, session_id,
                   original_event_id=d["original_event_id"], aspect=d["aspect"],
                   new_value=str(d["new_value"]), reason=d["reason"],
                   corrected_by=d["corrected_by"])
        return jsonify(corr), 201

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app


def _auth_out(auth: dict) -> dict:
    import json
    out = dict(auth)
    out["scope"] = json.loads(out.pop("scope_json")) if out["scope_json"] else None
    return out

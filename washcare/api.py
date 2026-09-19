"""Flask 移动端接口。

端点：
  GET  /api/health
  POST /api/precheck                         操作前核对
  GET  /api/babies/<baby_code>/consents       授权版本链
  POST /api/consents/narrow                  家长开始前缩小授权
  POST /api/consents/withdraw                家长撤回授权（进行中则立即中止）
  POST /api/sessions                         开始洗护（幂等）
  POST /api/sessions/<code>/events           追加事件（幂等）
  POST /api/sessions/<code>/corrections      事后更正（只追加）
  GET  /api/sessions/<code>/trace            主管追溯
  POST /api/admin/products/<code>/deactivate 中心停用产品
  POST /api/admin/lots/<code>/deactivate     中心停用/召回批次

所有写接口要求 JSON 并携带客户端幂等键 client_event_id
（product_change 另需 client_request_id 绑定消耗）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

from flask import Flask, g, jsonify, request

from . import db as dbmod
from .notifications import LogNotificationSender
from .service import ConflictError, ValidationError, WashCareService


def create_app(db_path: str | None = None, sender=None, *, seed: bool | None = None) -> Flask:
    app = Flask(__name__)
    db_path = db_path or os.environ.get("WASHCARE_DB", "washcare.db")
    fresh_file_db = db_path != ":memory:" and not os.path.exists(db_path)
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)
    # 显式 seed 优先；否则文件库首次创建时播种演示数据，内存库保持空白（测试自行控制）
    if seed is None:
        seed = fresh_file_db
    if seed:
        dbmod.seed_demo_data(conn)

    service = WashCareService(conn, sender or LogNotificationSender())
    # 共享单一连接（内存库尤须如此）：用可重入锁串行化每个请求，
    # 既满足 check_same_thread=False 下的线程安全，也避免 SQLite 写竞争。
    db_lock = threading.RLock()
    app.extensions["washcare"] = {"conn": conn, "service": service, "lock": db_lock}

    @app.before_request
    def _acquire_db_lock():
        db_lock.acquire()
        g.washcare_lock_held = True

    @app.teardown_request
    def _release_db_lock(exc):
        if g.pop("washcare_lock_held", False):
            db_lock.release()

    def payload() -> dict:
        if not request.is_json:
            raise ValidationError("json_required", "请求体必须是 application/json")
        return request.get_json(silent=True) or {}

    def require(data: dict, *fields: str):
        missing = [f for f in fields if data.get(f) in (None, "", [])]
        if missing:
            raise ValidationError("missing_fields", f"缺少必填字段：{missing}")

    @app.errorhandler(ValidationError)
    def _validation(err: ValidationError):
        return jsonify({"ok": False, "error": {"code": err.code, "message": str(err)}}), 400

    @app.errorhandler(ConflictError)
    def _conflict(err: ConflictError):
        return jsonify({"ok": False, "error": {"code": "conflict", "message": str(err)}}), 409

    @app.errorhandler(sqlite3.IntegrityError)
    def _integrity(err: sqlite3.IntegrityError):
        # 并发下唯一约束兜底（幂等键冲突等）
        return jsonify({"ok": False,
                        "error": {"code": "integrity_conflict", "message": str(err)}}), 409

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "service": "washcare"})

    @app.post("/api/precheck")
    def precheck():
        data = payload()
        require(data, "baby_code", "id_tag_observed", "caregiver_code")
        return jsonify(service.precheck(
            baby_code=data["baby_code"],
            id_tag_observed=data["id_tag_observed"],
            caregiver_code=data["caregiver_code"],
            planned_lot_codes=data.get("planned_lot_codes", [])))

    @app.get("/api/babies/<baby_code>/consents")
    def consent_chain(baby_code: str):
        rows = conn.execute(
            "SELECT c.* FROM consents c JOIN babies b ON b.id=c.baby_id"
            " WHERE b.baby_code=? ORDER BY c.version", (baby_code,)).fetchall()
        if not rows:
            raise ValidationError("consent_missing", "该婴儿暂无授权记录")
        return jsonify({"baby_code": baby_code, "versions": [
            {"version": r["version"], "status": r["status"], "source": r["source"],
             "all_products": bool(r["all_products"]),
             "scope": json.loads(r["scope_json"]),
             "note": r["note"], "created_at": r["created_at"]} for r in rows]})

    @app.post("/api/consents/narrow")
    def narrow():
        data = payload()
        if data.get("baby_code") in (None, "") or "allowed_product_codes" not in data \
                or data.get("note") in (None, ""):
            raise ValidationError("missing_fields",
                                  "缺少必填字段：baby_code / allowed_product_codes / note")
        return jsonify(service.narrow_consent(
            baby_code=data["baby_code"],
            allowed_product_codes=data["allowed_product_codes"],
            source=data.get("source", "app"), note=data["note"]))

    @app.post("/api/consents/withdraw")
    def withdraw():
        data = payload()
        require(data, "baby_code")
        return jsonify(service.withdraw_consent(
            baby_code=data["baby_code"], note=data.get("note", "")))

    @app.post("/api/sessions")
    def start():
        data = payload()
        require(data, "baby_code", "id_tag_observed", "caregiver_code", "client_event_id")
        return jsonify(service.start_session(
            baby_code=data["baby_code"], id_tag_observed=data["id_tag_observed"],
            caregiver_code=data["caregiver_code"],
            client_event_id=data["client_event_id"]))

    @app.post("/api/sessions/<session_code>/events")
    def events(session_code: str):
        data = payload()
        require(data, "type", "client_event_id")
        return jsonify(service.append_event(
            session_code=session_code, event_type=data["type"],
            client_event_id=data["client_event_id"], data=data.get("data", {})))

    @app.post("/api/sessions/<session_code>/corrections")
    def corrections(session_code: str):
        data = payload()
        require(data, "original_event_id", "client_event_id", "field")
        return jsonify(service.correct_event(
            session_code=session_code,
            original_event_id=int(data["original_event_id"]),
            client_event_id=data["client_event_id"],
            field=data["field"], new_value=data.get("new_value"),
            reason=data.get("reason", "")))

    @app.get("/api/sessions/<session_code>/trace")
    def trace(session_code: str):
        return jsonify(service.trace(session_code))

    @app.post("/api/admin/products/<product_code>/deactivate")
    def deactivate_product(product_code: str):
        data = payload()
        row = conn.execute("SELECT * FROM products WHERE product_code=?",
                           (product_code,)).fetchone()
        if row is None:
            raise ValidationError("product_not_found", f"产品 {product_code} 不存在")
        with conn:
            conn.execute("UPDATE products SET deactivated_at=? WHERE id=? AND deactivated_at IS NULL",
                         (dbmod.utcnow(), row["id"]))
        return jsonify({"ok": True, "product_code": product_code, "deactivated": True,
                        "reason": data.get("reason", "中心停用")})

    @app.post("/api/admin/lots/<lot_code>/deactivate")
    def deactivate_lot(lot_code: str):
        data = payload()
        row = conn.execute("SELECT * FROM lots WHERE lot_code=?", (lot_code,)).fetchone()
        if row is None:
            raise ValidationError("lot_not_found", f"批次 {lot_code} 不存在")
        with conn:
            conn.execute("UPDATE lots SET deactivated_at=? WHERE id=? AND deactivated_at IS NULL",
                         (dbmod.utcnow(), row["id"]))
        return jsonify({"ok": True, "lot_code": lot_code, "deactivated": True,
                        "reason": data.get("reason", "中心召回/停用")})

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000)

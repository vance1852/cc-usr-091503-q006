"""核心业务规则：操作前核对与洗护事件状态机。

规则汇总（对应需求）：
- 开始前必须同时通过：婴儿身份(扫码+第二标识一致)、护理人员资质(在有效期内)、
  家长授权(最新版本 active 且覆盖计划用品)、用品(产品/批次均未停用且未过期)。
- 授权缺失、身份不一致、用品停用/过期、资质失效一律不得开始。
- 家长可在开始前缩小授权范围（产生新版本，老版本保留）；撤回后不得开始，
  若已有进行中的洗护，撤回立即中止并进入复核、通知家长。
- 事件只追加：start / pause / resume / product_change / observation /
  abort / end / notification / correction，顺序由 seq 保证。
- 观察到不适迹象 -> 立即终止、会话进入 review、通知家长并保存回执；
  系统不下任何医学结论，只如实记录观察。
- 幂等：start 与各事件靠 client_event_id 去重；用品消耗靠
  (session, lot, client_request_id) 去重；同一会话重复扫同一批次
  不再生成操作、不再消耗。
- 更正只能追加 correction 事件并注明原记录，绝不修改原事件。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import utcnow
from .notifications import NotificationSender


class ValidationError(Exception):
    """业务规则拒绝（400），code 供移动端区分处理。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConflictError(Exception):
    """状态冲突（409），如对已结束的会话追加事件。"""


# --- 小工具 ----------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _lot_usable(lot: sqlite3.Row, product: sqlite3.Row, now: datetime) -> str | None:
    """返回不可用原因；None 表示可用。"""
    if product["deactivated_at"]:
        return f"产品 {product['product_code']} 已被中心停用"
    if lot["deactivated_at"]:
        return f"批次 {lot['lot_code']} 已被中心停用/召回"
    if _parse_dt(lot["expires_at"]) <= now:
        return f"批次 {lot['lot_code']} 已过有效期"
    return None


class WashCareService:
    def __init__(self, conn: sqlite3.Connection, sender: NotificationSender):
        self.conn = conn
        self.sender = sender

    # --- 查询 --------------------------------------------------------------

    def _baby(self, baby_code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM babies WHERE baby_code=?", (baby_code,)
        ).fetchone()
        if row is None:
            raise ValidationError("baby_not_found", f"未找到婴儿 {baby_code}")
        return row

    def _caregiver(self, staff_code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM caregivers WHERE staff_code=?", (staff_code,)
        ).fetchone()
        if row is None:
            raise ValidationError("caregiver_not_found", f"未找到护理人员 {staff_code}")
        return row

    def _latest_consent(self, baby_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM consents WHERE baby_id=? ORDER BY version DESC LIMIT 1",
            (baby_id,),
        ).fetchone()

    def _lot_with_product(self, lot_code: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        lot = self.conn.execute(
            "SELECT * FROM lots WHERE lot_code=?", (lot_code,)
        ).fetchone()
        if lot is None:
            raise ValidationError("lot_not_found", f"未找到用品批次 {lot_code}")
        product = self.conn.execute(
            "SELECT * FROM products WHERE id=?", (lot["product_id"],)
        ).fetchone()
        return lot, product

    def _open_session_for_baby(self, baby_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE baby_id=? AND status IN ('running','paused')",
            (baby_id,),
        ).fetchone()

    # --- 操作前核对（不落任何数据，正式 start 时还会在服务端重新核对） -------

    def precheck(
        self,
        *,
        baby_code: str,
        id_tag_observed: str,
        caregiver_code: str,
        planned_lot_codes: list[str],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        problems: list[dict[str, str]] = []

        baby = self._baby(baby_code)
        if id_tag_observed.strip() != baby["id_tag"].strip():
            problems.append({
                "code": "identity_mismatch",
                "message": "第二标识不一致：扫码婴儿与床头卡/手环不符，禁止开始",
            })

        caregiver = self._caregiver(caregiver_code)
        if not caregiver["active"]:
            problems.append({"code": "caregiver_inactive", "message": "护理人员账号已停用"})
        elif not (_parse_dt(caregiver["valid_from"]) <= now < _parse_dt(caregiver["valid_to"])):
            problems.append({
                "code": "qualification_expired",
                "message": f"资质 {caregiver['qualification']} 不在有效期内",
            })

        consent = self._latest_consent(baby["id"])
        scope: list[str] = []
        if consent is None:
            problems.append({"code": "consent_missing", "message": "查不到任何家长授权记录"})
        elif consent["status"] == "withdrawn":
            problems.append({
                "code": "consent_withdrawn",
                "message": f"家长已撤回授权（版本 {consent['version']}）",
            })
        else:
            scope = json.loads(consent["scope_json"])

        lots_view = []
        for lot_code in planned_lot_codes:
            lot, product = self._lot_with_product(lot_code)
            reason = _lot_usable(lot, product, now)
            if reason:
                problems.append({"code": "product_unavailable", "message": reason})
            if consent and consent["status"] == "active" and not consent["all_products"]:
                if product["product_code"] not in scope:
                    problems.append({
                        "code": "outside_consent_scope",
                        "message": f"用品 {product['product_code']} 不在家长授权范围内",
                    })
            lots_view.append({
                "lot_code": lot["lot_code"],
                "product_code": product["product_code"],
                "product_name": product["name"],
                "supplier": lot["supplier"],
                "expires_at": lot["expires_at"],
                "usable": reason is None,
            })

        if self._open_session_for_baby(baby["id"]):
            problems.append({
                "code": "session_already_open",
                "message": "该婴儿已有进行中的洗护，不能重复开始",
            })

        return {
            "ok": not problems,
            "problems": problems,
            "baby": {"baby_code": baby["baby_code"], "name": baby["name"],
                     "special_notes": baby["notes"]},
            "caregiver": {"staff_code": caregiver["staff_code"], "name": caregiver["name"],
                          "qualification": caregiver["qualification"],
                          "valid_to": caregiver["valid_to"]},
            "consent": None if consent is None else {
                "version": consent["version"], "status": consent["status"],
                "all_products": bool(consent["all_products"]),
                "scope": scope if consent["status"] == "active" else [],
                "source": consent["source"], "note": consent["note"],
            },
            "planned_lots": lots_view,
            "checked_at": utcnow(),
        }

    # --- 授权变更（仅允许开始前缩小；撤回随时） -----------------------------

    def narrow_consent(
        self, *, baby_code: str, allowed_product_codes: list[str], source: str, note: str
    ) -> dict[str, Any]:
        baby = self._baby(baby_code)
        if self._open_session_for_baby(baby["id"]):
            raise ConflictError("洗护已开始，不能再变更授权范围；如需收回请使用撤回")
        current = self._latest_consent(baby["id"])
        if current is None:
            raise ValidationError("consent_missing", "尚无授权可缩小")
        if current["status"] == "withdrawn":
            raise ValidationError("consent_withdrawn", "授权已被撤回，无法缩小")

        current_scope = set(json.loads(current["scope_json"]))
        requested = set(allowed_product_codes)
        unknown = requested - {
            r["product_code"] for r in self.conn.execute("SELECT product_code FROM products")
        }
        if unknown:
            raise ValidationError("unknown_product", f"未知产品：{sorted(unknown)}")
        if not requested:
            raise ValidationError("empty_scope", "缩小后的授权不能为空（如需全部收回请撤回）")
        if not requested <= current_scope:
            raise ValidationError(
                "scope_not_subset",
                f"只能缩小不能扩大；新增项：{sorted(requested - current_scope)}",
            )

        ts = utcnow()
        with self.conn:
            self.conn.execute(
                "UPDATE consents SET status='narrowed' WHERE id=?", (current["id"],)
            )
            cur = self.conn.execute(
                "INSERT INTO consents (baby_id,version,scope_json,all_products,source,status,"
                "note,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (baby["id"], current["version"] + 1, json.dumps(sorted(requested)), 0,
                 source, "active", f"家长在操作前缩小授权范围。{note}", ts),
            )
            new_id = cur.lastrowid
        return {"baby_code": baby_code, "version": current["version"] + 1,
                "scope": sorted(requested), "consent_id": new_id}

    def withdraw_consent(self, *, baby_code: str, note: str) -> dict[str, Any]:
        baby = self._baby(baby_code)
        current = self._latest_consent(baby["id"])
        if current is None:
            raise ValidationError("consent_missing", "尚无授权可撤回")
        if current["status"] == "withdrawn":
            return {"baby_code": baby_code, "version": current["version"], "already": True}

        ts = utcnow()
        with self.conn:
            self.conn.execute(
                "UPDATE consents SET status='superseded' WHERE id=?", (current["id"],)
            )
            cur = self.conn.execute(
                "INSERT INTO consents (baby_id,version,scope_json,all_products,source,status,"
                "note,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (baby["id"], current["version"] + 1, current["scope_json"],
                 current["all_products"], "app", "withdrawn", f"家长撤回授权。{note}", ts),
            )
            version = current["version"] + 1
            session = self._open_session_for_baby(baby["id"])
            aborted = None
            if session is not None:
                # 进行中撤回：立即终止并复核、通知家长
                aborted = self._abort(
                    session,
                    reason_code="consent_withdrawn",
                    reason="家长在洗护过程中撤回授权，立即终止操作",
                    notify_subject="授权撤回，洗护已中止",
                    notify_content=f"家长撤回了 {baby['name']} 本次洗护的授权，"
                                   "操作已立即终止并进入主管复核。",
                    lock_held=True)
        return {"baby_code": baby_code, "version": version,
                "aborted_session": aborted}

    # --- 会话开始 / 事件追加 -------------------------------------------------

    def start_session(
        self,
        *,
        baby_code: str,
        id_tag_observed: str,
        caregiver_code: str,
        client_event_id: str,
    ) -> dict[str, Any]:
        baby = self._baby(baby_code)

        # 幂等重传：同一 client_event_id 直接回放原结果
        existing = self._find_idempotent_event(client_event_id)
        if existing:
            session = self.conn.execute(
                "SELECT * FROM sessions WHERE id=?", (existing["session_id"],)
            ).fetchone()
            result = self._event_result(existing, replayed=True)
            result["session_code"] = session["session_code"]
            return result

        now = datetime.now(timezone.utc)
        if id_tag_observed.strip() != baby["id_tag"].strip():
            raise ValidationError("identity_mismatch",
                                  "身份核对失败：第二标识与登记不一致，禁止开始")
        caregiver = self._caregiver(caregiver_code)
        if not caregiver["active"] or not (
            _parse_dt(caregiver["valid_from"]) <= now < _parse_dt(caregiver["valid_to"])
        ):
            raise ValidationError("qualification_invalid", "护理人员资质无效或已过期")

        consent = self._latest_consent(baby["id"])
        if consent is None:
            raise ValidationError("consent_missing", "缺少家长授权，禁止开始")
        if consent["status"] != "active":
            raise ValidationError("consent_not_active",
                                  f"授权状态为 {consent['status']}，禁止开始")

        if self._open_session_for_baby(baby["id"]):
            raise ConflictError("该婴儿已有进行中的洗护，重复扫码不能生成第二次操作")

        ts = utcnow()
        with self.conn:
            scur = self.conn.execute(
                "INSERT INTO sessions (session_code,baby_id,caregiver_id,consent_version,"
                "status,id_confirmed,started_at) VALUES (?,?,?,?,?,1,?)",
                (f"SESS-{baby['id']:04d}-{ts.replace('-','').replace(':','')[0:8]}-"
                 f"{client_event_id[:6].upper()}",
                 baby["id"], caregiver["id"], consent["version"], "running", ts),
            )
            session_id = scur.lastrowid
            event = self._insert_event(
                session_id, "start",
                {"baby_code": baby["baby_code"], "id_tag": id_tag_observed,
                 "caregiver": caregiver["staff_code"], "consent_version": consent["version"],
                 "consent_scope": json.loads(consent["scope_json"]),
                 "all_products": bool(consent["all_products"]),
                 "special_notes": baby["notes"]},
                client_event_id, ts,
            )
        session = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        result = self._event_result(event)
        result["session_code"] = session["session_code"]
        return result

    def append_event(
        self, *, session_code: str, event_type: str, client_event_id: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = data or {}
        session = self.conn.execute(
            "SELECT * FROM sessions WHERE session_code=?", (session_code,)
        ).fetchone()
        if session is None:
            raise ValidationError("session_not_found", f"会话 {session_code} 不存在")

        existing = self._find_idempotent_event(client_event_id, session["id"])
        if existing:
            return self._event_result(existing, replayed=True)

        if session["status"] not in ("running", "paused"):
            raise ConflictError(f"会话已处于 {session['status']}，不能追加事件")

        handler = {
            "pause": self._ev_pause,
            "resume": self._ev_resume,
            "product_change": self._ev_product_change,
            "observation": self._ev_observation,
            "end": self._ev_end,
            "abort": self._ev_abort,
        }.get(event_type)
        if handler is None:
            raise ValidationError("unknown_event_type", f"不支持的事件类型 {event_type}")
        return handler(session, data, client_event_id)

    # -- 各事件处理 ----------------------------------------------------------

    def _ev_pause(self, session, data, client_event_id) -> dict[str, Any]:
        if session["status"] != "running":
            raise ConflictError("只有进行中的会话可以暂停")
        with self.conn:
            self.conn.execute("UPDATE sessions SET status='paused' WHERE id=?", (session["id"],))
            event = self._insert_event(
                session["id"], "pause", {"reason": data.get("reason", "")}, client_event_id)
        return self._event_result(event)

    def _ev_resume(self, session, data, client_event_id) -> dict[str, Any]:
        if session["status"] != "paused":
            raise ConflictError("只有暂停中的会话可以恢复")
        with self.conn:
            self.conn.execute("UPDATE sessions SET status='running' WHERE id=?", (session["id"],))
            event = self._insert_event(
                session["id"], "resume", {"note": data.get("note", "")}, client_event_id)
        return self._event_result(event)

    def _ev_product_change(self, session, data, client_event_id) -> dict[str, Any]:
        if session["status"] != "running":
            raise ConflictError("只有进行中才能更换/取用用品")
        lot_code = data.get("lot_code")
        client_request_id = data.get("client_request_id")
        if not lot_code or not client_request_id:
            raise ValidationError("missing_fields", "product_change 需要 lot_code 和 client_request_id")

        lot, product = self._lot_with_product(lot_code)
        now = datetime.now(timezone.utc)
        reason = _lot_usable(lot, product, now)
        if reason:
            raise ValidationError("product_unavailable", reason)

        consent = self.conn.execute(
            "SELECT * FROM consents WHERE baby_id=? AND version=?",
            (session["baby_id"], session["consent_version"]),
        ).fetchone()
        if consent["status"] != "active" or (
            not consent["all_products"]
            and product["product_code"] not in json.loads(consent["scope_json"])
        ):
            raise ValidationError("outside_consent_scope",
                                  f"用品 {product['product_code']} 不在本次授权（版本"
                                  f"{consent['version']}）范围内，禁止更换")

        # 重复扫码：同一会话同一批次已有消耗记录，直接回放，不再生成操作
        prior = self.conn.execute(
            "SELECT event_id FROM lot_consumptions WHERE session_id=? AND lot_id=?",
            (session["id"], lot["id"]),
        ).fetchone()
        if prior:
            ev = self.conn.execute("SELECT * FROM events WHERE id=?",
                                   (prior["event_id"],)).fetchone()
            result = self._event_result(ev, replayed=True)
            result["already_scanned"] = True
            return result

        if lot["consumed_qty"] + 1 > lot["initial_qty"]:
            raise ValidationError("lot_exhausted", f"批次 {lot['lot_code']} 库存不足")

        prior_use = self.conn.execute(
            "SELECT payload_json FROM events WHERE session_id=? AND type='product_change'"
            " ORDER BY seq DESC LIMIT 1", (session["id"],),
        ).fetchone()
        from_lot = json.loads(prior_use["payload_json"]).get("lot_code") if prior_use else None

        ts = utcnow()
        with self.conn:
            event = self._insert_event(
                session["id"], "product_change",
                {"from_lot": from_lot, "lot_code": lot["lot_code"],
                 "product_code": product["product_code"], "product_name": product["name"],
                 "supplier": lot["supplier"], "reason": data.get("reason", ""),
                 "client_request_id": client_request_id},
                client_event_id, ts)
            # 唯一约束 (session, lot, client_request_id) 兜住网络重传
            self.conn.execute(
                "INSERT INTO lot_consumptions (session_id,lot_id,client_request_id,qty,"
                "event_id,consumed_at) VALUES (?,?,?,1,?,?)",
                (session["id"], lot["id"], client_request_id, event["id"], ts))
            self.conn.execute("UPDATE lots SET consumed_qty=consumed_qty+1 WHERE id=?",
                              (lot["id"],))
        return self._event_result(event)

    def _ev_observation(self, session, data, client_event_id) -> dict[str, Any]:
        signs = data.get("signs") or []
        note = data.get("note", "")
        discomfort = bool(data.get("discomfort"))
        ts = utcnow()
        with self.conn:
            event = self._insert_event(
                session["id"], "observation",
                {"signs": signs, "discomfort": discomfort, "note": note,
                 "medical_judgment": None,  # 系统明确不做医学判断
                 "observer": data.get("observer", "")},
                client_event_id, ts)
            extra = None
            if discomfort:
                # 不适迹象：立即终止 + 复核 + 通知家长
                baby = self.conn.execute("SELECT * FROM babies WHERE id=?",
                                         (session["baby_id"],)).fetchone()
                extra = self._abort(
                    session,
                    reason_code="discomfort_signs",
                    reason=f"现场观察到不适迹象 {signs}，按规程立即终止并复核",
                    notify_subject="洗护中出现不适迹象，已中止并进入复核",
                    notify_content=(f"{baby['name']} 在洗护过程中现场观察到：{signs}。"
                                    f"备注：{note}。操作已立即终止并启动主管复核，"
                                    "本通知仅为现场观察告知，不构成医学判断。"),
                    ts=ts, lock_held=True)
        result = self._event_result(event)
        if extra:
            result["auto_abort"] = extra
        return result

    def _ev_end(self, session, data, client_event_id) -> dict[str, Any]:
        ts = utcnow()
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET status='ended', ended_at=? WHERE id=?",
                (ts, session["id"]))
            event = self._insert_event(
                session["id"], "end", {"note": data.get("note", "")}, client_event_id, ts)
        return self._event_result(event)

    def _ev_abort(self, session, data, client_event_id) -> dict[str, Any]:
        with self.conn:
            obs_event = self._insert_event(
                session["id"], "observation",
                {"signs": data.get("signs", []), "note": data.get("reason", "手动终止"),
                 "discomfort": bool(data.get("discomfort", False))},
                client_event_id + ":obs", utcnow())
            aborted = self._abort(
                session, reason_code=data.get("reason_code", "manual_abort"),
                reason=data.get("reason", "现场手动终止"),
                notify_subject="洗护已中止",
                notify_content=data.get("notify_content",
                                        "本次洗护已由现场人员中止并进入复核。"),
                lock_held=True, abort_event_key=client_event_id)
        return {"event": self._event_json(obs_event), "abort": aborted}

    # -- 中止 + 复核 + 通知（统一出口） ---------------------------------------

    def _abort(self, session, *, reason_code: str, reason: str,
               notify_subject: str, notify_content: str,
               ts: str | None = None, lock_held: bool = False,
               abort_event_key: str | None = None) -> dict[str, Any]:
        ts = ts or utcnow()

        def work():
            self.conn.execute(
                "UPDATE sessions SET status='review', ended_at=? WHERE id=?",
                (ts, session["id"]))
            abort_event = self._insert_event(
                session["id"], "abort",
                {"reason_code": reason_code, "reason": reason, "review_required": True},
                abort_event_key or f"abort:{reason_code}:{session['id']}:{ts}", ts)
            note = self._notify_and_record(
                session, subject=notify_subject, content=notify_content,
                related_event_id=abort_event["id"], ts=ts)
            return {"session_code": session["session_code"], "status": "review",
                    "reason_code": reason_code, "reason": reason,
                    "abort_event_id": abort_event["id"], "notification": note}

        if lock_held:
            return work()
        with self.conn:
            return work()

    def _notify_and_record(self, session, *, subject, content, related_event_id=None,
                           ts=None) -> dict[str, Any]:
        ts = ts or utcnow()
        baby = self.conn.execute("SELECT * FROM babies WHERE id=?",
                                 (session["baby_id"],)).fetchone()
        recipient = f"PARENT({baby['baby_code']})"
        resp = self.sender.send(channel="sms+app", recipient=recipient,
                                subject=subject, content=content)
        ncur = self.conn.execute(
            "INSERT INTO notifications (session_id,channel,recipient,subject,content,status,"
            "receipt,event_id,sent_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session["id"], "sms+app", recipient, subject, content,
             resp["status"], resp.get("receipt", ""), related_event_id,
             resp.get("sent_at", ts)))
        notification_id = ncur.lastrowid
        self._insert_event(
            session["id"], "notification",
            {"notification_id": notification_id, "channel": "sms+app",
             "recipient": recipient, "subject": subject, "status": resp["status"],
             "receipt": resp.get("receipt", "")},
            f"notify:{notification_id}", ts)
        return {"id": notification_id, "status": resp["status"],
                "receipt": resp.get("receipt", "")}

    # --- 更正（只追加，必须注明原记录） --------------------------------------

    def correct_event(self, *, session_code: str, original_event_id: int,
                      client_event_id: str, field: str, new_value: Any,
                      reason: str) -> dict[str, Any]:
        if not reason:
            raise ValidationError("reason_required", "事后更正必须注明原因")
        session = self.conn.execute(
            "SELECT * FROM sessions WHERE session_code=?", (session_code,)
        ).fetchone()
        if session is None:
            raise ValidationError("session_not_found", "会话不存在")
        original = self.conn.execute(
            "SELECT * FROM events WHERE id=? AND session_id=?",
            (original_event_id, session["id"]),
        ).fetchone()
        if original is None:
            raise ValidationError("original_event_not_found",
                                  "原记录不存在或不属于该会话；更正必须指向真实原记录")
        old_value = json.loads(original["payload_json"]).get(field)
        with self.conn:
            event = self._insert_event(
                session["id"], "correction",
                {"original_event_id": original_event_id,
                 "original_type": original["type"], "field": field,
                 "old_value": old_value, "new_value": new_value,
                 "reason": reason},
                client_event_id, utcnow())
        return self._event_result(event)

    # --- 主管追溯 -----------------------------------------------------------

    def trace(self, session_code: str) -> dict[str, Any]:
        session = self.conn.execute(
            "SELECT s.*, b.baby_code, b.name AS baby_name, b.notes AS special_notes,"
            " cg.staff_code, cg.name AS caregiver_name, cg.qualification"
            " FROM sessions s JOIN babies b ON b.id=s.baby_id"
            " JOIN caregivers cg ON cg.id=s.caregiver_id"
            " WHERE s.session_code=?", (session_code,),
        ).fetchone()
        if session is None:
            raise ValidationError("session_not_found", "会话不存在")
        consent = self.conn.execute(
            "SELECT * FROM consents WHERE baby_id=? AND version=?",
            (session["baby_id"], session["consent_version"]),
        ).fetchone()
        events = [self._event_json(e) for e in self.conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session["id"],))]
        consumptions = [dict(r) for r in self.conn.execute(
            "SELECT c.id, l.lot_code, p.product_code, p.name AS product_name, l.supplier,"
            " l.expires_at, c.qty, c.client_request_id, c.consumed_at, e.seq AS event_seq"
            " FROM lot_consumptions c JOIN lots l ON l.id=c.lot_id"
            " JOIN products p ON p.id=l.product_id JOIN events e ON e.id=c.event_id"
            " WHERE c.session_id=? ORDER BY c.id", (session["id"],))]
        notifications = [dict(r) for r in self.conn.execute(
            "SELECT id,channel,recipient,subject,content,status,receipt,sent_at"
            " FROM notifications WHERE session_id=? ORDER BY id", (session["id"],))]
        return {
            "session_code": session["session_code"],
            "status": session["status"],
            "baby": {"baby_code": session["baby_code"], "name": session["baby_name"],
                     "special_notes": session["special_notes"],
                     "identity_confirmed": bool(session["id_confirmed"])},
            "caregiver": {"staff_code": session["staff_code"],
                          "name": session["caregiver_name"],
                          "qualification": session["qualification"]},
            "consent_locked": {
                "version": consent["version"], "status": consent["status"],
                "source": consent["source"],
                "scope": json.loads(consent["scope_json"]),
                "all_products": bool(consent["all_products"]),
                "note": consent["note"], "created_at": consent["created_at"],
            },
            "started_at": session["started_at"], "ended_at": session["ended_at"],
            "events": events,
            "product_sources": consumptions,
            "notifications": notifications,
        }

    # --- 内部辅助 -----------------------------------------------------------

    def _next_seq(self, session_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS s FROM events WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row["s"]

    def _insert_event(self, session_id, event_type, payload, client_event_id,
                      ts=None) -> sqlite3.Row:
        ts = ts or utcnow()
        cur = self.conn.execute(
            "INSERT INTO events (session_id,seq,client_event_id,type,payload_json,recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, self._next_seq(session_id), client_event_id, event_type,
             json.dumps(payload, ensure_ascii=False, default=str), ts),
        )
        return self.conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()

    def _find_idempotent_event(self, client_event_id: str,
                               session_id: int | None = None) -> sqlite3.Row | None:
        if not client_event_id:
            return None
        if session_id is not None:
            return self.conn.execute(
                "SELECT * FROM events WHERE session_id=? AND client_event_id=?",
                (session_id, client_event_id),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM events WHERE client_event_id=? AND type='start'",
            (client_event_id,),
        ).fetchone()

    @staticmethod
    def _event_json(event: sqlite3.Row) -> dict[str, Any]:
        return {"id": event["id"], "seq": event["seq"], "type": event["type"],
                "client_event_id": event["client_event_id"],
                "payload": json.loads(event["payload_json"]),
                "recorded_at": event["recorded_at"]}

    def _event_result(self, event: sqlite3.Row, replayed: bool = False) -> dict[str, Any]:
        result = {"event": self._event_json(event)}
        if replayed:
            result["replayed"] = True
        return result

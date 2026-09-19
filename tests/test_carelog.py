"""核心场景测试：开始前核对、中途换品、授权撤回、网络重传、事后更正与追溯。"""


def _auth(client, baby_id, granted_by="家长", scope=None):
    return client.post("/api/authorizations",
                       json={"baby_id": baby_id, "granted_by": granted_by,
                             "scope": scope})


def _start(client, seed, idem="idem-1", wristband="WB-001",
           acknowledged=True, scans=None, baby_id=None):
    scans = scans if scans is not None else [
        {"product_id": seed["p1"], "scan_token": "scan-A"}]
    return client.post("/api/sessions", json={
        "idempotency_key": idem, "baby_id": baby_id or seed["baby_id"],
        "wristband_code": wristband, "staff_id": seed["staff_id"],
        "note_acknowledged": acknowledged, "scans": scans})


# ------------------------------------------------------------ 开始前核对

def test_missing_authorization_blocks_start(client, seed):
    """纸面默认勾选不等于系统授权：无有效授权不得开始。"""
    r = _start(client, seed)
    assert r.status_code == 403
    assert r.get_json()["error"] == "authorization_missing"


def test_identity_mismatch_blocks_start(client, seed):
    _auth(client, seed["baby_id"])
    r = _start(client, seed, wristband="WB-999")
    assert r.status_code == 409
    assert r.get_json()["error"] == "identity_mismatch"


def test_special_notes_must_be_acknowledged(client, seed):
    _auth(client, seed["baby_id"])
    r = _start(client, seed, acknowledged=False)
    assert r.status_code == 403
    assert r.get_json()["error"] == "special_notes_unacknowledged"


def test_deactivated_product_blocks_start(app, client, seed):
    _auth(client, seed["baby_id"])
    r = client.post(f"/api/products/{seed['p1']}/deactivate")
    assert r.status_code == 200
    r = _start(client, seed)
    assert r.status_code == 409
    assert r.get_json()["error"] == "product_deactivated"


def test_expired_staff_blocks_start(app, client, seed):
    _auth(client, seed["baby_id"])
    with app.app_context():
        from carelog.db import get_db
        get_db().execute(
            "UPDATE staff SET valid_until='2020-01-01' WHERE id=?",
            (seed["staff_id"],))
        get_db().commit()
    r = _start(client, seed)
    assert r.status_code == 403
    assert r.get_json()["error"] == "staff_qualification_expired"


def test_product_outside_narrowed_scope_blocks_start(client, seed):
    _auth(client, seed["baby_id"])
    # 家长操作前只保留 p2（原范围为全部）
    r = client.post(f"/api/babies/{seed['baby_id']}/authorization/narrow",
                    json={"granted_by": "家长", "scope": {"product_ids": [seed["p2"]]}})
    assert r.status_code == 201 and r.get_json()["version"] == 2
    # 使用 p1 开始必须被拒
    r = _start(client, seed)
    assert r.status_code == 403
    assert r.get_json()["error"] == "product_not_authorized"


def test_narrow_cannot_expand_scope(client, seed):
    _auth(client, seed["baby_id"], scope={"product_ids": [seed["p1"]]})
    r = client.post(f"/api/babies/{seed['baby_id']}/authorization/narrow",
                    json={"granted_by": "家长",
                          "scope": {"product_ids": [seed["p1"], seed["p2"]]}})
    assert r.status_code == 400
    assert r.get_json()["error"] == "scope_not_subset"


# ------------------------------------------------------------ 网络重传（幂等）

def test_start_network_retry_is_idempotent(client, seed):
    _auth(client, seed["baby_id"])
    body = {
        "idempotency_key": "idem-77", "baby_id": seed["baby_id"],
        "wristband_code": "WB-001", "staff_id": seed["staff_id"],
        "note_acknowledged": True,
        "scans": [{"product_id": seed["p1"], "scan_token": "scan-A"}],
    }
    r1 = client.post("/api/sessions", json=body)
    r2 = client.post("/api/sessions", json=body)  # 移动端断网后自动重传
    assert r1.status_code == 201 and r1.get_json()["created"] is True
    assert r2.status_code == 200 and r2.get_json()["created"] is False
    assert r1.get_json()["session"]["id"] == r2.get_json()["session"]["id"]

    trace = client.get(f"/api/sessions/{r1.get_json()['session']['id']}").get_json()
    # 只产生一次 start 事件、一次用品消耗
    assert [e["type"] for e in trace["events"]] == ["start"]
    assert len(trace["product_uses"]) == 1


def test_duplicate_scan_cannot_consume_twice(client, seed):
    """同一用品扫码不能在两次洗护中重复消耗。"""
    _auth(client, seed["baby_id"])
    r1 = _start(client, seed, idem="s1")
    assert r1.status_code == 201
    # 正常结束第一次
    sid1 = r1.get_json()["session"]["id"]
    assert client.post(f"/api/sessions/{sid1}/events",
                       json={"type": "end"}).status_code == 201

    r2 = _start(client, seed, idem="s2", scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}])  # 复用同一扫码
    assert r2.status_code == 409
    assert r2.get_json()["error"] == "scan_token_consumed"


def test_event_retry_is_idempotent(client, seed):
    _auth(client, seed["baby_id"])
    sid = _start(client, seed).get_json()["session"]["id"]
    payload = {"type": "pause", "client_event_id": "evt-pause-1", "payload": {}}
    r1 = client.post(f"/api/sessions/{sid}/events", json=payload)
    r2 = client.post(f"/api/sessions/{sid}/events", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["id"] == r2.get_json()["id"]
    trace = client.get(f"/api/sessions/{sid}").get_json()
    assert [e["type"] for e in trace["events"]] == ["start", "pause"]


# ------------------------------------------------------------ 中途换品

def test_mid_session_product_change(client, seed):
    """操作中途换品：按实际顺序留痕、新批次可溯源，重放不重复。"""
    _auth(client, seed["baby_id"])
    sid = _start(client, seed, idem="chg-1", scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}]).get_json()["session"]["id"]

    change = {"type": "product_change", "client_event_id": "evt-chg-1",
              "payload": {"product_id": seed["p2"], "scan_token": "scan-B",
                          "reason": "原用品余量不足"}}
    r1 = client.post(f"/api/sessions/{sid}/events", json=change)
    r2 = client.post(f"/api/sessions/{sid}/events", json=change)  # 网络重传
    assert r1.status_code == 201 and r2.get_json()["id"] == r1.get_json()["id"]

    # 同一扫码再扫一次也不能重复消耗
    again = {"type": "product_change", "client_event_id": "evt-chg-2",
             "payload": {"product_id": seed["p2"], "scan_token": "scan-B"}}
    r3 = client.post(f"/api/sessions/{sid}/events", json=again)
    assert r3.status_code == 201 and r3.get_json()["id"] == r1.get_json()["id"]

    trace = client.get(f"/api/sessions/{sid}").get_json()
    assert [e["type"] for e in trace["events"]] == ["start", "product_change"]
    assert len(trace["product_uses"]) == 2
    by_token = {u["scan_token"]: u for u in trace["product_uses"]}
    assert by_token["scan-A"]["batch_no"] == "B20260901"
    assert by_token["scan-B"]["batch_no"] == "B20260902"
    assert by_token["scan-B"]["supplier"] == "安护用品厂"
    # 事件严格有序
    seq = [e["seq"] for e in trace["events"]]
    assert seq == sorted(seq) and len(set(seq)) == len(seq)


def test_product_change_rejects_unauthorized_product(client, seed):
    _auth(client, seed["baby_id"])
    sid = _start(client, seed, scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}]).get_json()["session"]["id"]
    # 将授权缩小到 p1（操作开始前未缩，会话内快照为全部——此处用会话内快照校验，
    # 换 p2 本应允许；改为预先缩小后再开始的场景）
    client.post(f"/api/sessions/{sid}/events", json={"type": "end"})
    client.post(f"/api/babies/{seed['baby_id']}/authorization/narrow",
                json={"granted_by": "家长", "scope": {"product_ids": [seed["p1"]]}})
    sid2 = _start(client, seed, idem="chg-2", scans=[
        {"product_id": seed["p1"], "scan_token": "scan-C"}]).get_json()["session"]["id"]
    r = client.post(f"/api/sessions/{sid2}/events", json={
        "type": "product_change",
        "payload": {"product_id": seed["p2"], "scan_token": "scan-D"}})
    assert r.status_code == 403
    assert r.get_json()["error"] == "product_not_authorized"


def test_narrow_blocked_during_session(client, seed):
    _auth(client, seed["baby_id"])
    _start(client, seed)
    r = client.post(f"/api/babies/{seed['baby_id']}/authorization/narrow",
                    json={"granted_by": "家长", "scope": {"product_ids": [seed["p1"]]}})
    assert r.status_code == 409
    assert r.get_json()["error"] == "session_in_progress"


# ------------------------------------------------------------ 授权撤回与不适处置

def test_withdrawal_mid_session_forces_abort_and_review(client, seed):
    _auth(client, seed["baby_id"])
    sid = _start(client, seed, scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}]).get_json()["session"]["id"]
    auth_id = client.get(f"/api/sessions/{sid}").get_json()["session"]["auth_id"]

    # 家长临时表示不放心并撤回授权
    r = client.post(f"/api/authorizations/{auth_id}/withdraw")
    assert r.status_code == 200 and r.get_json()["status"] == "withdrawn"

    # 继续普通操作被拒绝，要求立即终止
    r = client.post(f"/api/sessions/{sid}/events", json={"type": "pause"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "authorization_withdrawn"

    r = client.post(f"/api/sessions/{sid}/abort",
                    json={"reason": "guardian_withdrawal",
                          "note": "家长撤回授权，立即停止洗护",
                          "guardian_contact": "家长-微信"})
    assert r.status_code == 201

    trace = client.get(f"/api/sessions/{sid}").get_json()
    assert trace["session"]["status"] == "aborted"
    assert len(trace["reviews"]) == 1
    assert trace["reviews"][0]["reason"] == "guardian_withdrawal"
    assert len(trace["notifications"]) == 1
    n = trace["notifications"][0]
    assert n["recipient"] == "家长-微信" and n["status"] == "sent"
    assert n["receipt"]["message_id"].startswith("msg-")

    # 终止后不能再写事件
    r = client.post(f"/api/sessions/{sid}/events", json={"type": "end"})
    assert r.status_code == 409

    # 家长确认回执，主管完成复核
    assert client.post(f"/api/notifications/{n['id']}/confirm").status_code == 200
    r = client.post(f"/api/reviews/{trace['reviews'][0]['id']}/resolve",
                    json={"conclusion": "停用相关用品，改为清水护理"})
    assert r.status_code == 200
    trace2 = client.get(f"/api/sessions/{sid}").get_json()
    assert trace2["notifications"][0]["status"] == "confirmed"
    assert trace2["reviews"][0]["status"] == "resolved"


def test_discomfort_observation_auto_aborts(client, seed):
    """现场记录不适迹象：系统不做医学判断，但必须立即终止、复核、通知。"""
    _auth(client, seed["baby_id"])
    sid = _start(client, seed, scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}]).get_json()["session"]["id"]
    r = client.post(f"/api/sessions/{sid}/events", json={
        "type": "observation",
        "payload": {"discomfort": True, "signs": ["局部泛红", "哭闹"],
                    "note": "涂抹润肤乳后约两分钟出现", "guardian_contact": "家长"}})
    assert r.status_code == 201

    trace = client.get(f"/api/sessions/{sid}").get_json()
    assert trace["session"]["status"] == "aborted"
    assert trace["session"]["end_reason"] == "discomfort_signs"
    types = [e["type"] for e in trace["events"]]
    assert types == ["start", "observation", "end"]
    assert trace["reviews"][0]["observation_event_id"] == trace["events"][1]["id"]
    assert len(trace["notifications"]) == 1
    # 观察内容原样保留
    assert trace["events"][1]["payload"]["signs"] == ["局部泛红", "哭闹"]


def test_cannot_start_after_authorization_withdrawn(client, seed):
    r = _auth(client, seed["baby_id"])
    auth_id = r.get_json()["id"]
    client.post(f"/api/authorizations/{auth_id}/withdraw")
    r = _start(client, seed)
    assert r.status_code == 403
    assert r.get_json()["error"] == "authorization_missing"


# ------------------------------------------------------------ 事后更正与全链路追溯

def test_correction_append_only_requires_reason(client, seed):
    _auth(client, seed["baby_id"])
    sid = _start(client, seed).get_json()["session"]["id"]
    change = client.post(f"/api/sessions/{sid}/events", json={
        "type": "product_change",
        "payload": {"product_id": seed["p2"], "scan_token": "scan-B",
                    "reason": "余量不足"}}).get_json()

    # 无原因的更正被拒绝
    r = client.post(f"/api/sessions/{sid}/corrections", json={
        "original_event_id": change["id"], "aspect": "reason",
        "new_value": "家长要求更换", "reason": "", "corrected_by": "主管王"})
    assert r.status_code == 400

    r = client.post(f"/api/sessions/{sid}/corrections", json={
        "original_event_id": change["id"], "aspect": "reason",
        "new_value": "家长要求更换", "reason": "现场笔误，主管复核监控后更正",
        "corrected_by": "主管王"})
    assert r.status_code == 201
    corr = r.get_json()
    assert corr["old_value"] == "余量不足"
    assert corr["new_value"] == "家长要求更换"

    # 原始事件保持不变，更正单独留痕
    trace = client.get(f"/api/sessions/{sid}").get_json()
    event = next(e for e in trace["events"] if e["id"] == change["id"])
    assert event["payload"]["reason"] == "余量不足"
    assert len(trace["corrections"]) == 1


def test_full_traceability(client, seed):
    """主管从一次洗护可追溯授权链、执行步骤、用品来源、异常处置、通知状态。"""
    _auth(client, seed["baby_id"])
    client.post(f"/api/babies/{seed['baby_id']}/authorization/narrow",
                json={"granted_by": "家长",
                      "scope": {"product_ids": [seed["p1"], seed["p2"]]}})
    sid = _start(client, seed, idem="trace-1", scans=[
        {"product_id": seed["p1"], "scan_token": "scan-A"}]).get_json()["session"]["id"]
    client.post(f"/api/sessions/{sid}/events",
                json={"type": "pause", "client_event_id": "e1"})
    client.post(f"/api/sessions/{sid}/events",
                json={"type": "resume", "client_event_id": "e2"})
    client.post(f"/api/sessions/{sid}/events", json={
        "type": "product_change", "client_event_id": "e3",
        "payload": {"product_id": seed["p2"], "scan_token": "scan-B"}})
    client.post(f"/api/sessions/{sid}/events",
                json={"type": "end", "client_event_id": "e4",
                      "payload": {"reason": "completed"}})

    t = client.get(f"/api/sessions/{sid}").get_json()
    assert t["baby"]["wristband_code"] == "WB-001"
    assert t["staff"]["qualification_no"] == "NURSE-2026-01"
    assert [a["version"] for a in t["authorization_chain"]] == [1, 2]
    assert t["session"]["auth_version"] == 2
    assert [e["type"] for e in t["events"]] == [
        "start", "pause", "resume", "product_change", "end"]
    assert {u["batch_no"] for u in t["product_uses"]} == {"B20260901", "B20260902"}
    assert t["special_notes_at_start"][0]["content"].startswith("面颊轻度湿疹")
    assert t["session"]["status"] == "ended"

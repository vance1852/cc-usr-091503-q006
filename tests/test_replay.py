"""网络重传与重复扫码的幂等性。"""

import conftest
from conftest import BABY, ID_TAG, LOT_LOTION, LOT_WASH, NURSE_OK


def _start(client, event_id="evt-start-1"):
    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "client_event_id": event_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["session_code"]


def _change(client, code, event_id, req_id, lot=LOT_WASH, reason=""):
    return client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": event_id,
        "data": {"lot_code": lot, "client_request_id": req_id, "reason": reason}})


def test_start_network_retry_is_idempotent(client):
    body = {"baby_code": BABY, "id_tag_observed": ID_TAG,
            "caregiver_code": NURSE_OK, "client_event_id": "evt-mobile-001"}
    r1 = client.post("/api/sessions", json=body)
    r2 = client.post("/api/sessions", json=body)  # 移动端超时后原样重发
    assert r1.get_json()["session_code"] == r2.get_json()["session_code"]
    assert r2.get_json()["replayed"] is True

    # 库里只有一个会话、一个 start 事件
    conn = client.application.extensions["washcare"]["conn"]
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE type='start'").fetchone()["c"] == 1


def test_product_change_replay_does_not_double_consume(client):
    """中途换品请求网络重传：只消耗一次、只产生一条事件。"""
    code = _start(client)
    body = {"type": "product_change", "client_event_id": "evt-pc-77",
            "data": {"lot_code": LOT_WASH, "client_request_id": "req-77",
                     "reason": "首次取用沐浴露"}}
    r1 = client.post(f"/api/sessions/{code}/events", json=body)
    r2 = client.post(f"/api/sessions/{code}/events", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json()["event"]["id"] == r2.get_json()["event"]["id"]
    assert r2.get_json()["replayed"] is True

    conn = client.application.extensions["washcare"]["conn"]
    lot = conn.execute("SELECT consumed_qty FROM lots WHERE lot_code=?",
                       (LOT_WASH,)).fetchone()
    assert lot["consumed_qty"] == 1
    n = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE session_id="
        "(SELECT id FROM sessions WHERE session_code=?) AND type='product_change'",
        (code,)).fetchone()["c"]
    assert n == 1


def test_rescan_same_lot_with_new_request_does_not_create_second_op(client):
    """重复扫同一个批次（即便换了请求号）也不能重复消耗或生成两次操作。"""
    code = _start(client)
    assert _change(client, code, "evt-pc-a", "req-a").status_code == 200
    r = _change(client, code, "evt-pc-b", "req-b")
    assert r.status_code == 200
    assert r.get_json()["already_scanned"] is True
    # 回放的是第一次的事件，不新增
    assert r.get_json()["event"]["client_event_id"] == "evt-pc-a"

    conn = client.application.extensions["washcare"]["conn"]
    assert conn.execute("SELECT consumed_qty FROM lots WHERE lot_code=?",
                        (LOT_WASH,)).fetchone()["consumed_qty"] == 1


def test_mid_session_product_change_records_order_and_source(client):
    """重放中途换品：start -> wash 批次 -> 换 lotion 批次，顺序与来源可追溯。"""
    code = _start(client, "evt-replay-1")
    assert _change(client, code, "evt-pc-wash", "req-wash", LOT_WASH,
                   "先清洁").status_code == 200
    client.post(f"/api/sessions/{code}/events", json={
        "type": "pause", "client_event_id": "evt-pause-1",
        "data": {"reason": "婴儿哭闹，暂停安抚"}})
    client.post(f"/api/sessions/{code}/events", json={
        "type": "resume", "client_event_id": "evt-resume-1"})
    assert _change(client, code, "evt-pc-lotion", "req-lotion", LOT_LOTION,
                   "清洁后涂抹润肤乳").status_code == 200
    client.post(f"/api/sessions/{code}/events", json={
        "type": "end", "client_event_id": "evt-end-1"})

    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert [e["type"] for e in trace["events"]] == [
        "start", "product_change", "pause", "resume", "product_change", "end"]
    # 第二条换品记录指向第一条（from_lot），来源链完整
    changes = [e for e in trace["events"] if e["type"] == "product_change"]
    assert changes[0]["payload"]["lot_code"] == LOT_WASH
    assert changes[1]["payload"]["from_lot"] == LOT_WASH
    assert changes[1]["payload"]["lot_code"] == LOT_LOTION

    sources = {s["lot_code"]: s for s in trace["product_sources"]}
    assert set(sources) == {LOT_WASH, LOT_LOTION}
    assert sources[LOT_LOTION]["supplier"] == "安护日化供应商"
    assert trace["status"] == "ended"


def test_pause_resume_replay_idempotent(client):
    code = _start(client)
    body = {"type": "pause", "client_event_id": "evt-pause-x",
            "data": {"reason": "接听家长电话"}}
    r1 = client.post(f"/api/sessions/{code}/events", json=body)
    r2 = client.post(f"/api/sessions/{code}/events", json=body)
    assert r1.get_json()["event"]["id"] == r2.get_json()["event"]["id"]
    # 暂停态再次 pause 冲突；重传不会冲突
    r3 = client.post(f"/api/sessions/{code}/events", json={
        "type": "pause", "client_event_id": "evt-pause-other"})
    assert r3.status_code == 409


def test_retry_with_same_event_id_but_different_payload_returns_original(client):
    """同幂等键不同内容：以首次请求为准，拒绝内容漂移。"""
    code = _start(client)
    r1 = _change(client, code, "evt-same", "req-same", LOT_WASH)
    r2 = _change(client, code, "evt-same", "req-same", LOT_LOTION)
    assert r1.get_json()["event"]["payload"]["lot_code"] == LOT_WASH
    assert r2.get_json()["event"]["payload"]["lot_code"] == LOT_WASH

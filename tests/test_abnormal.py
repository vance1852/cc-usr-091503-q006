"""不适迹象终止复核、通知回执、失败通道、事后更正。"""

from conftest import BABY, ID_TAG, LOT_WASH, NURSE_OK


def _start(client, event_id="evt-ab-1"):
    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "client_event_id": event_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["session_code"]


def _use_wash(client, code, eid="evt-ab-pc", req="req-ab"):
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": eid,
        "data": {"lot_code": LOT_WASH, "client_request_id": req}})
    assert r.status_code == 200


def test_discomfort_observation_aborts_and_notifies(client):
    code = _start(client)
    _use_wash(client, code)

    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "observation", "client_event_id": "evt-obs-1",
        "data": {"signs": ["耳后发红", "哭闹加剧"], "discomfort": True,
                 "observer": "王育婴",
                 "note": "涂抹沐浴露约两分钟后出现，立即用清水冲净"}})
    assert r.status_code == 200
    body = r.get_json()
    # 观察事件本身不做医学判断
    assert body["event"]["payload"]["medical_judgment"] is None
    auto = body["auto_abort"]
    assert auto["status"] == "review"
    assert auto["reason_code"] == "discomfort_signs"
    assert auto["notification"]["status"] == "sent"
    assert auto["notification"]["receipt"].startswith("SMS+APP-")

    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert trace["status"] == "review"
    types = [e["type"] for e in trace["events"]]
    assert types == ["start", "product_change", "observation", "abort", "notification"]
    # 通知回执可追溯
    note = trace["notifications"][0]
    assert note["status"] == "sent" and note["receipt"]
    assert "不构成医学判断" in note["content"]
    # 复核后不能再继续操作或正常结束
    assert client.post(f"/api/sessions/{code}/events", json={
        "type": "end", "client_event_id": "evt-end-x"}).status_code == 409


def test_plain_observation_does_not_abort(client):
    code = _start(client)
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "observation", "client_event_id": "evt-obs-ok",
        "data": {"signs": ["情绪平稳"], "discomfort": False, "note": "常规观察"}})
    assert r.status_code == 200
    assert "auto_abort" not in r.get_json()
    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert trace["status"] == "running"


def test_failed_notification_still_recorded(failing_client):
    """通道故障：失败回执也落库，主管可见通知未送达。"""
    code = _start(failing_client)
    _use_wash(failing_client, code)
    r = failing_client.post(f"/api/sessions/{code}/events", json={
        "type": "observation", "client_event_id": "evt-obs-f",
        "data": {"signs": ["皮疹样红斑"], "discomfort": True}})
    assert r.status_code == 200
    assert r.get_json()["auto_abort"]["notification"]["status"] == "failed"

    trace = failing_client.get(f"/api/sessions/{code}/trace").get_json()
    assert trace["status"] == "review"
    assert trace["notifications"][0]["status"] == "failed"
    # 事件流里同样留有失败的通知事件
    note_events = [e for e in trace["events"] if e["type"] == "notification"]
    assert note_events[0]["payload"]["status"] == "failed"


def test_abort_observation_replay_is_idempotent(client):
    """中止瞬间网络重传：不能重复中止、不能重复通知。"""
    code = _start(client)
    _use_wash(client, code)
    body = {"type": "observation", "client_event_id": "evt-obs-r",
            "data": {"signs": ["呛咳"], "discomfort": True}}
    r1 = client.post(f"/api/sessions/{code}/events", json=body)
    r2 = client.post(f"/api/sessions/{code}/events", json=body)
    assert r1.status_code == 200
    assert r2.get_json()["replayed"] is True

    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert len(trace["notifications"]) == 1
    assert [e["type"] for e in trace["events"]] == [
        "start", "product_change", "observation", "abort", "notification"]


def test_correction_appended_with_reference_and_reason(client):
    """事后更正：只追加 correction，原记录不动，必须注明原记录与原因。"""
    code = _start(client)
    pc = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": "evt-pc-c",
        "data": {"lot_code": LOT_WASH, "client_request_id": "req-c",
                 "reason": "初次清洁"}})
    original_id = pc.get_json()["event"]["id"]

    # 没有原因不允许更正
    r = client.post(f"/api/sessions/{code}/corrections", json={
        "original_event_id": original_id, "client_event_id": "evt-corr-bad",
        "field": "reason", "new_value": "x", "reason": ""})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "reason_required"

    r = client.post(f"/api/sessions/{code}/corrections", json={
        "original_event_id": original_id, "client_event_id": "evt-corr-1",
        "field": "reason", "new_value": "实际为耳后局部清洁，非全身",
        "reason": "现场记录填写有误，主管复核后更正"})
    assert r.status_code == 200
    payload = r.get_json()["event"]["payload"]
    assert payload["original_event_id"] == original_id
    assert payload["old_value"] == "初次清洁"
    assert payload["new_value"].startswith("实际为")

    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    original = next(e for e in trace["events"] if e["id"] == original_id)
    # 原记录保持不变
    assert original["payload"]["reason"] == "初次清洁"
    assert trace["events"][-1]["type"] == "correction"


def test_manual_abort_enters_review(client):
    code = _start(client)
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "abort", "client_event_id": "evt-manual-abort",
        "data": {"reason": "水温设备故障，无法继续", "signs": []}})
    assert r.status_code == 200
    assert r.get_json()["abort"]["status"] == "review"
    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert trace["status"] == "review"
    assert trace["notifications"][0]["status"] == "sent"

"""授权缩小、撤回与版本链。"""

from conftest import BABY, ID_TAG, LOT_WASH, NURSE_OK


def _start(client, event_id):
    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "client_event_id": event_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["session_code"]


def test_verbal_concern_narrow_before_start(client):
    """纸面单勾选了全部默认用品，家长口头不放心按摩油：开始前缩小授权。"""
    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY,
        "allowed_product_codes": ["P-WASH", "P-LOTION"],
        "source": "verbal_confirmed_on_device",
        "note": "家长对按摩油不放心，现场口头确认本次不用 P-OIL"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["version"] == 2

    # 版本链保留 v1（纸面单）与 v2（缩小后）
    chain = client.get(f"/api/babies/{BABY}/consents").get_json()["versions"]
    assert [v["version"] for v in chain] == [1, 2]
    assert chain[0]["all_products"] is True
    assert chain[1]["status"] == "active"
    assert "P-OIL" not in chain[1]["scope"]

    # 核对时 P-OIL 落在授权范围外
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK,
        "planned_lot_codes": ["LOT-OIL-2025-03"]})
    codes = [p["code"] for p in r.get_json()["problems"]]
    assert "outside_consent_scope" in codes


def test_narrow_cannot_enlarge_or_empty(client):
    # 未知产品
    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY,
        "allowed_product_codes": ["P-WASH", "P-LOTION", "P-OIL", "P-OTHER"],
        "note": "夹带未知产品"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "unknown_product"

    # 已知产品但属于"扩大范围"：先缩小，再试图加回 P-OIL
    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY, "allowed_product_codes": ["P-WASH"],
        "note": "先缩小"})
    assert r.status_code == 200
    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY, "allowed_product_codes": ["P-WASH", "P-OIL"],
        "note": "试图把刚去掉的加回来"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "scope_not_subset"

    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY, "allowed_product_codes": [],
        "note": "试图清空"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "empty_scope"


def test_narrow_blocked_after_session_started(client):
    _start(client, "evt-n-1")
    r = client.post("/api/consents/narrow", json={
        "baby_code": BABY, "allowed_product_codes": ["P-WASH"],
        "note": "中途想改范围"})
    assert r.status_code == 409


def test_withdraw_before_start_blocks_session(client):
    r = client.post("/api/consents/withdraw", json={
        "baby_code": BABY, "note": "家长临时取消本次洗护"})
    assert r.status_code == 200
    assert r.get_json()["version"] == 2

    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "planned_lot_codes": [LOT_WASH]})
    codes = [p["code"] for p in r.get_json()["problems"]]
    assert "consent_withdrawn" in codes

    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "client_event_id": "evt-w-1"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "consent_not_active"


def test_withdraw_during_running_session_aborts_and_notifies(client):
    """操作进行中家长撤回：立即终止、进入复核、通知家长并留回执。"""
    code = _start(client, "evt-wd-1")
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": "evt-wd-pc",
        "data": {"lot_code": LOT_WASH, "client_request_id": "req-wd"}})
    assert r.status_code == 200

    r = client.post("/api/consents/withdraw", json={
        "baby_code": BABY, "note": "家长电话要求停止"})
    assert r.status_code == 200
    aborted = r.get_json()["aborted_session"]
    assert aborted["status"] == "review"
    assert aborted["reason_code"] == "consent_withdrawn"
    assert aborted["notification"]["status"] == "sent"
    assert aborted["notification"]["receipt"]

    trace = client.get(f"/api/sessions/{code}/trace").get_json()
    assert trace["status"] == "review"
    types = [e["type"] for e in trace["events"]]
    assert types == ["start", "product_change", "abort", "notification"]
    # 撤回后不能再追加事件
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "end", "client_event_id": "evt-wd-end"})
    assert r.status_code == 409

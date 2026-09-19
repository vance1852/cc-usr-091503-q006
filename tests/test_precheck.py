"""核对规则：身份、资质、授权、用品停用、口头疑虑场景。"""

from conftest import (BABY, ID_TAG, LOT_LOTION, LOT_OIL_EXPIRED, LOT_WASH,
                      NURSE_EXPIRED, NURSE_OK)


def _start(client, event_id, baby=BABY, tag=ID_TAG, nurse=NURSE_OK):
    r = client.post("/api/sessions", json={
        "baby_code": baby, "id_tag_observed": tag,
        "caregiver_code": nurse, "client_event_id": event_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["session_code"]


def test_precheck_ok_with_all_default_products(client):
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK,
        "planned_lot_codes": [LOT_WASH, LOT_LOTION]})
    body = r.get_json()
    assert body["ok"] is True
    assert body["problems"] == []
    # 特殊注意事项必须在核对结果里带到现场
    assert "湿疹" in body["baby"]["special_notes"]
    assert body["consent"]["version"] == 1


def test_identity_mismatch_blocks_start(client):
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": "手环 W-9999 / 错的床头卡",
        "caregiver_code": NURSE_OK, "planned_lot_codes": [LOT_WASH]})
    codes = [p["code"] for p in r.get_json()["problems"]]
    assert "identity_mismatch" in codes

    # 即使强行调开始接口也必须拒绝
    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": "错的标识",
        "caregiver_code": NURSE_OK, "client_event_id": "evt-forged-1"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "identity_mismatch"


def test_expired_qualification_blocks(client):
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_EXPIRED, "planned_lot_codes": [LOT_WASH]})
    codes = [p["code"] for p in r.get_json()["problems"]]
    assert "qualification_expired" in codes

    r = client.post("/api/sessions", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_EXPIRED, "client_event_id": "evt-q-1"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "qualification_invalid"


def test_expired_lot_and_deactivated_product_block(client):
    # 过期批次
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "planned_lot_codes": [LOT_OIL_EXPIRED]})
    codes = [p["code"] for p in r.get_json()["problems"]]
    assert "product_unavailable" in codes

    # 中心停用产品后，对应批次也不可用
    assert client.post("/api/admin/products/P-WASH/deactivate",
                       json={"reason": "厂家通报"}).status_code == 200
    r = client.post("/api/precheck", json={
        "baby_code": BABY, "id_tag_observed": ID_TAG,
        "caregiver_code": NURSE_OK, "planned_lot_codes": [LOT_WASH]})
    problems = r.get_json()["problems"]
    assert any("已被中心停用" in p["message"] for p in problems)

    # 开始后取用被停用的批次也必须拒绝
    code = _start(client, "evt-blocked-1")
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": "evt-pc-blocked",
        "data": {"lot_code": LOT_WASH, "client_request_id": "req-blocked"}})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "product_unavailable"


def test_lot_deactivation_after_start_blocks_mid_change(client):
    """开始后批次才被中心停用：已用的不追溯，再换新批次被拒绝。"""
    code = _start(client, "evt-deact-1")
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": "evt-pc-1",
        "data": {"lot_code": LOT_WASH, "client_request_id": "req-1"}})
    assert r.status_code == 200

    assert client.post("/api/admin/lots/LOT-LOTION-2026-09/deactivate",
                       json={"reason": "批次召回"}).status_code == 200
    r = client.post(f"/api/sessions/{code}/events", json={
        "type": "product_change", "client_event_id": "evt-pc-2",
        "data": {"lot_code": LOT_LOTION, "client_request_id": "req-2"}})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "product_unavailable"


def test_baby_not_found(client):
    r = client.post("/api/sessions", json={
        "baby_code": "NO-SUCH-BABY", "id_tag_observed": "x",
        "caregiver_code": "x", "client_event_id": "evt-x"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "baby_not_found"

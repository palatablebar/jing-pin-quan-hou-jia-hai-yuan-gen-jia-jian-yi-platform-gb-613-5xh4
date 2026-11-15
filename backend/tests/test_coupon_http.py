"""券核对接口的 HTTP 层测试：权限、幂等、依据失效即重算。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts import seed_demo
from scripts.demo_config import build_dataset
from src.utils.security import hash_password
from src.utils.timeutil import local_now


@pytest.fixture()
def client(db, monkeypatch):
    from src.config import app as app_module

    monkeypatch.setattr(app_module, "engine", db.get_bind())

    from src.models.user import User
    from scripts.demo_config import DEMO_PASSWORD, DEMO_USERS, ORG

    for email, display_name, role in DEMO_USERS:
        db.add(User(email=email, password_hash=hash_password(DEMO_PASSWORD), display_name=display_name, role=role, org=ORG))
    db.flush()
    dataset = build_dataset()
    competitors = seed_demo.ensure_competitors(db, dataset["competitors"])
    skus = seed_demo.ensure_skus(db, dataset["skus"][:40], competitors)
    seed_demo.ensure_rules(db, dataset["rules"], skus)
    keep = {item["sku_code"] for item in dataset["skus"][:40]}
    seed_demo.ensure_coupons(db, dataset["coupons"], competitors, local_now().date())
    seed_demo.ensure_snapshots(
        db,
        [item for item in dataset["snapshots"] if item["sku_code"] in keep],
        skus,
        competitors,
        local_now().date(),
    )
    db.commit()

    from src.services import pricing_service

    pricing_service.compute_run(db, captured_on=local_now().date(), operator_email="admin@pricewatch.io", trigger="seed")
    db.commit()

    app = app_module.create_app()
    with TestClient(app) as test_client:
        yield test_client


def _auth(client, email, password="Demo@2026"):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_coupon_check_requires_write_role(client):
    admin = _auth(client, "admin@pricewatch.io")
    viewer = _auth(client, "viewer@pricewatch.io")
    item = client.get(
        "/api/snapshots", headers=admin, params={"captured_on": local_now().date().isoformat(), "q": "PB-sofa-001"}
    ).json()["items"][0]
    assert client.post(f"/api/snapshots/{item['id']}/coupon-check", headers=viewer, json={}).status_code == 403
    # 未登录 401
    assert client.post(f"/api/snapshots/{item['id']}/coupon-check", json={}).status_code == 401
    # 券台账只读：viewer 可读不可改
    coupon_id = client.get("/api/coupons", headers=viewer).json()["items"][0]["id"]
    assert client.get(f"/api/coupons/{coupon_id}", headers=viewer).status_code == 200
    assert client.patch(f"/api/coupons/{coupon_id}", headers=viewer, json={"total_quota": 0}).status_code == 403


def test_coupon_check_rerun_voids_old_basis_and_keeps_claims_idempotent(client):
    admin = _auth(client, "admin@pricewatch.io")
    analyst = _auth(client, "analyst@pricewatch.io")
    today = local_now().date().isoformat()
    item = client.get("/api/snapshots", headers=admin, params={"captured_on": today, "q": "PB-sofa-001"}).json()["items"][0]
    assert item["basis_status"] == "valid"
    assert item["visible_unit_price_1"] == 279.0

    # 券被抢空
    coupon = client.get("/api/coupons", headers=admin, params={"q": "dy-muyan-shop-coupon"}).json()["items"][0]
    patched = client.patch(
        f"/api/coupons/{coupon['id']}",
        headers=admin,
        json={"total_quota": coupon["claimed_count"], "reason": "午间券被抢空"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "exhausted"

    checked = client.post(
        f"/api/snapshots/{item['id']}/coupon-check", headers=analyst, json={"reason": "午间复核"}
    ).json()
    payload = checked["coupon_check"]
    assert payload["invalidated"] is True
    assert payload["basis_status"] == "invalidated"
    assert checked["visible_unit_price_1"] == 299.0  # 店铺券抢空，1 件不再按券面价 279
    assert "抢空" in checked["basis_reason"]
    assert payload["rerun"] is not None
    assert payload["rerun"]["voided_advices"] > 0
    assert payload["rerun"]["claims_new"] == 0

    advices = client.get("/api/advices", headers=admin, params={"q": "PB-sofa-001", "limit": 20}).json()["items"]
    voided = [row for row in advices if row["status"] == "voided"]
    active = [row for row in advices if row["status"] == "pending"]
    assert voided and active
    assert voided[0]["superseded_by_id"] == active[0]["id"]
    assert voided[0]["basis_status"] == "invalidated"
    assert active[0]["basis_status"] == "valid"
    assert active[0]["p25"] >= 299.0

    # 作废建议不允许审批
    response = client.patch(f"/api/advices/{voided[0]['id']}/decision", headers=admin, json={"status": "approved"})
    assert response.status_code == 409
    assert response.json()["code"] == "BASIS_INVALIDATED"
    assert response.json()["detail"]["superseded_by_id"] == active[0]["id"]

    # 再次核对同一快照：不得再领券、不得再占名额
    before = client.get(f"/api/coupons/{coupon['id']}", headers=admin).json()
    again = client.post(f"/api/snapshots/{item['id']}/coupon-check", headers=analyst, json={}).json()
    after = client.get(f"/api/coupons/{coupon['id']}", headers=admin).json()
    assert after["claimed_count"] == before["claimed_count"]
    assert again["coupon_check"]["claims"]["claimed"] == 0

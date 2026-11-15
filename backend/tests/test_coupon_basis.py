"""券台账 / 领取权 / 依据失效重算的集成测试（SQLite + 真实服务层）。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.models.advice import Advice
from src.models.coupon import Coupon, CouponClaim, CouponEvent, SnapshotCoupon
from src.models.snapshot import Snapshot
from src.services import advice_service, coupon_service, pricing_service, snapshot_service
from src.utils.errors import AppError
from src.utils.timeutil import local_now

from scripts.demo_config import DEMO_USERS, build_dataset
from scripts import seed_demo


def _seed(db, sku_count: int | None = None):
    """种一份与生产同口径的演示数据（可裁 SKU 数量加快测试）。"""
    from src.models.competitor import Competitor
    from src.models.rule import Rule
    from src.models.sku import Sku
    from src.models.user import User
    from scripts.demo_config import DEMO_PASSWORD, ORG

    for email, display_name, role in DEMO_USERS:
        db.add(User(email=email, password_hash="x", display_name=display_name, role=role, org=ORG))
    db.flush()
    dataset = build_dataset()
    if sku_count:
        keep = {item["sku_code"] for item in dataset["skus"][:sku_count]}
        dataset["skus"] = [item for item in dataset["skus"] if item["sku_code"] in keep]
        dataset["snapshots"] = [item for item in dataset["snapshots"] if item["sku_code"] in keep]
    competitors = seed_demo.ensure_competitors(db, dataset["competitors"])
    skus = seed_demo.ensure_skus(db, dataset["skus"], competitors)
    seed_demo.ensure_rules(db, dataset["rules"], skus)
    today = local_now().date()
    seed_demo.ensure_coupons(db, dataset["coupons"], competitors, today)
    seed_demo.ensure_snapshots(db, dataset["snapshots"], skus, competitors, today)
    return dataset, competitors, skus, today


def _run(db, key=None):
    today = local_now().date()
    return pricing_service.compute_run(
        db, captured_on=today, operator_email="analyst@pricewatch.io", trigger="manual", seed_key=key
    )


def test_seeded_run_keeps_baseline_counts(db):
    dataset, competitors, skus, today = _seed(db)
    run = _run(db)
    assert run.status == "success"
    assert run.output_count == 42
    assert run.metrics["high"] == 9 and run.metrics["medium"] == 13 and run.metrics["low"] == 20
    assert run.metrics["invalidated_snapshots"] == 0
    assert len(dataset["coupons"]) == 12
    # 首轮：每张被快照引用到的券只领一次（同一账号 + 同一抓价日幂等），不按 SKU 数累加
    claimed_coupons = {row.coupon_id for row in db.query(CouponClaim).filter(CouponClaim.result == "claimed")}
    assert len(claimed_coupons) == db.query(CouponClaim).count()
    linked_coupons = {row.coupon_id for row in db.query(SnapshotCoupon)}
    assert claimed_coupons <= linked_coupons
    assert run.metrics["claims_new"] == len(claimed_coupons)
    # 抓价口径与可见价一致（券都还能领）
    snapshot = db.query(Snapshot).filter(Snapshot.captured_on == today, Snapshot.shop_coupon.isnot(None)).first()
    assert snapshot.effective_visible["unit_price_1"] == snapshot.effective["unit_price_1"]
    assert snapshot.basis_status == "valid"


def test_rerun_same_capture_does_not_claim_twice(db):
    _seed(db)
    _run(db)
    claims_before = {(row.coupon_id, row.account_email): row.attempts for row in db.query(CouponClaim)}
    totals_before = {coupon.code: coupon.claimed_count for coupon in db.query(Coupon)}
    run2 = _run(db)
    totals_after = {coupon.code: coupon.claimed_count for coupon in db.query(Coupon)}
    assert totals_after == totals_before, "重跑同一抓价不得再占名额"
    assert db.query(CouponClaim).count() == len(claims_before)
    claims_after = {(row.coupon_id, row.account_email): row.attempts for row in db.query(CouponClaim)}
    assert all(claims_after[key] > claims_before[key] for key in claims_before), "重跑应累加 attempts 留痕"
    assert run2.metrics["claims_new"] == 0
    assert run2.metrics["claims_reused"] > 0
    assert run2.output_count == 42


def test_coupon_exhausted_invalidates_basis_and_reprices_on_visible_price(db):
    _seed(db)
    first = _run(db)
    coupon = db.query(Coupon).filter_by(code="dy-muyan-shop-coupon").one()
    before = {"status": coupon.status, "remaining": coupon.remaining, "eligible": coupon.eligible}
    coupon_service.apply_state_change(
        db, coupon, {"total_quota": coupon.claimed_count}, reason="午间券被抢空", actor_email="admin@pricewatch.io", actor_role="admin"
    )
    db.flush()

    second = _run(db)
    assert second.metrics["invalidated_snapshots"] > 0
    assert second.metrics["priced_on_visible"] > 0
    assert second.metrics["voided_advices"] > 0

    snapshot = (
        db.query(Snapshot)
        .filter(Snapshot.sku_id.in_(db.query(Advice.sku_id).filter(Advice.run_id == second.id)))
        .first()
    )
    # 抓价口径原样保留（复盘早班看到的价格），可见价剔除失效券后重算
    assert snapshot.effective["unit_price_1"] is not None
    assert snapshot.effective_visible["degraded"] is True
    assert snapshot.basis_status == "invalidated"
    assert "抢空" in snapshot.basis_reason
    assert snapshot.effective_visible["unit_price_1"] >= snapshot.effective["unit_price_1"]

    # 新建议按可见价出，且标记依据更新
    new_advice = db.query(Advice).filter(Advice.run_id == second.id, Advice.sku_id == snapshot.sku_id).one()
    assert new_advice.basis_status == "valid"
    assert new_advice.payload["snapshot_basis_status"] == "invalidated"
    assert new_advice.payload["priced_on"] == "effective_visible"
    assert new_advice.p25 >= snapshot.effective_visible["unit_price_1"] - 0.01

    # 旧建议作废，并指向新建议作为去向
    old_advice = db.query(Advice).filter(Advice.run_id == first.id, Advice.sku_id == snapshot.sku_id).one()
    assert old_advice.status == "voided"
    assert old_advice.basis_status == "invalidated"
    assert old_advice.superseded_by_id == new_advice.id
    assert old_advice.payload["resolution"]

    # 券状态变化留痕前后值
    events = db.query(CouponEvent).filter(CouponEvent.coupon_id == coupon.id, CouponEvent.event == "update").all()
    assert events
    assert events[-1].before_remaining != events[-1].after_remaining or events[-1].before_status != events[-1].after_status
    assert events[-1].reason == "午间券被抢空"
    assert before["remaining"] > 0


def test_voided_advice_cannot_be_approved(db):
    _seed(db)
    _run(db)
    coupon = db.query(Coupon).filter_by(code="dy-muyan-shop-coupon").one()
    coupon_service.apply_state_change(
        db, coupon, {"total_quota": coupon.claimed_count}, reason="午间券被抢空", actor_email="admin@pricewatch.io", actor_role="admin"
    )
    db.flush()
    _run(db)
    voided = db.query(Advice).filter(Advice.status == "voided").first()
    assert voided is not None
    with pytest.raises(AppError) as excinfo:
        advice_service.decide(
            db, voided, status="approved", note=None, approver="admin@pricewatch.io", approved_price=None
        )
    assert excinfo.value.code == "BASIS_INVALIDATED"
    assert excinfo.value.detail["superseded_by_id"] == voided.superseded_by_id


def test_account_eligibility_loss_blocks_claims_and_invalidates(db):
    _seed(db)
    _run(db)
    coupon = db.query(Coupon).filter_by(code="tb-beian-shop-coupon").one()
    coupon_service.apply_state_change(
        db,
        coupon,
        {"eligible": False},
        reason="抓价账号被判定不满足新客资格",
        actor_email="admin@pricewatch.io",
        actor_role="admin",
    )
    db.flush()
    run = _run(db)
    assert run.metrics["claims_blocked"] > 0
    link = (
        db.query(SnapshotCoupon)
        .filter(SnapshotCoupon.coupon_id == coupon.id)
        .order_by(SnapshotCoupon.id.desc())
        .first()
    )
    assert link.claimable is False
    assert "资格" in (link.reason or "")
    assert link.status_now in ("active", "exhausted")
    assert db.query(Advice).filter(Advice.run_id == run.id).count() > 0


def test_per_account_limit_exhausted_is_reported(db):
    _seed(db)
    coupon = db.query(Coupon).filter_by(code="jd-yuanmu-shop-coupon").one()
    coupon.per_account_limit = 1
    db.flush()
    _run(db)
    # 第二天再抓一次：账号可领次数已用尽，应拦下并留痕
    tomorrow = local_now().date() + timedelta(days=1)
    from src.models.competitor import Competitor

    competitor = db.get(Competitor, coupon.competitor_id)
    snapshot = db.query(Snapshot).filter(Snapshot.competitor_id == coupon.competitor_id).first()
    claim, status, reason, consumed = coupon_service.resolve_claim(
        db, coupon, account=coupon_service.account_of(competitor), sku_id=snapshot.sku_id, captured_on=tomorrow
    )
    assert status == "limit_reached"
    assert consumed is False
    assert "用尽" in reason
    assert claim.result == "limit_reached"


def test_snapshot_refresh_is_idempotent_and_reports_visible_price(db):
    _seed(db)
    _run(db)
    snapshot = db.query(Snapshot).filter(Snapshot.shop_coupon.isnot(None)).first()
    totals_before = {coupon.code: coupon.claimed_count for coupon in db.query(Coupon)}
    info = snapshot_service.refresh(db, snapshot)
    totals_after = {coupon.code: coupon.claimed_count for coupon in db.query(Coupon)}
    assert totals_after == totals_before, "页面核对当前可用价不应重复领券"
    assert info["claims"]["claimed"] == 0
    data = snapshot_service.serialize(db, snapshot, "viewer", detail=True)
    assert data["visible_unit_price_1"] == data["unit_price_1"]
    assert data["basis_status"] == "valid"
    assert data["coupons"], "快照应能列出引用的券、领取状态与名额"
    payload = data["coupons"][0]
    for field in ("status_at_capture", "status_now", "claim_status", "remaining_at_capture", "remaining_now", "claimable"):
        assert field in payload


def test_manual_repair_snapshot_gets_coupon_basis(db):
    """手工补录快照也要挂上券台账，才能在午间核对依据是否失效。"""
    _seed(db, sku_count=5)
    from src.models.competitor import Competitor
    from src.models.sku import Sku

    sku = db.query(Sku).filter_by(sku_code="PB-sofa-001").one()
    competitor = db.query(Competitor).filter_by(id=sku.competitor_id).one()
    snapshot = Snapshot(
        sku_id=sku.id,
        competitor_id=competitor.id,
        captured_on=local_now().date() - timedelta(days=30),
        page_price=299.0,
        full_reduction=[{"threshold": 300.0, "minus": 40.0}],
        shop_coupon={"threshold": 200.0, "minus": 20.0, "stackable": True},
        platform_coupon=None,
        member_discount=1.0,
        effective={"valid": True, "unit_price_1": 279.0},
        campaign="手工补录",
    )
    db.add(snapshot)
    db.flush()
    snapshot_service.ensure_coupon_links(db, snapshot)
    db.flush()
    link = db.query(SnapshotCoupon).filter_by(snapshot_id=snapshot.id).one()
    assert link.coupon_id is not None
    assert link.status_now == "active"
    assert snapshot.effective_visible["unit_price_1"] == 279.0
    assert snapshot.basis_status == "valid"

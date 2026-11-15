"""抓价快照服务：入库时即做券后价还原（算法 A），列表/详情按角色脱敏。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import CATEGORY_LABELS, PLATFORM_LABELS
from src.models.competitor import Competitor
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import effective_price
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc


def spec_of(snapshot: Snapshot) -> dict:
    return {
        "page_price": snapshot.page_price,
        "full_reduction": snapshot.full_reduction or [],
        "shop_coupon": snapshot.shop_coupon,
        "platform_coupon": snapshot.platform_coupon,
        "member_discount": snapshot.member_discount,
    }


def serialize(db: Session, snapshot: Snapshot, role: str = "viewer", *, detail: bool = False) -> dict:
    sku = db.get(Sku, snapshot.sku_id)
    competitor = db.get(Competitor, snapshot.competitor_id) if snapshot.competitor_id else None
    effective = snapshot.effective or {}
    visible = snapshot.effective_visible or {}
    coupons = _coupon_links(db, snapshot, detail=detail)
    data = {
        "id": snapshot.id,
        "sku_id": snapshot.sku_id,
        "sku_code": sku.sku_code if sku else None,
        "sku_name": sku.name if sku else None,
        "category": sku.category if sku else None,
        "category_label": CATEGORY_LABELS.get(sku.category, sku.category) if sku else None,
        "competitor_id": snapshot.competitor_id,
        "competitor_name": competitor.name if competitor else None,
        "competitor_code": competitor.code if competitor else None,
        "platform_label": PLATFORM_LABELS.get(competitor.platform, competitor.platform) if competitor else None,
        "captured_on": snapshot.captured_on.isoformat() if snapshot.captured_on else None,
        "page_price": round(snapshot.page_price, 2),
        "full_reduction": snapshot.full_reduction or [],
        "shop_coupon": snapshot.shop_coupon,
        "platform_coupon": snapshot.platform_coupon,
        "member_discount": snapshot.member_discount,
        "stock": snapshot.stock,
        "campaign": snapshot.campaign,
        "source_url": snapshot.source_url,
        "unit_price_1": effective.get("unit_price_1"),
        "unit_price_2": effective.get("unit_price_2"),
        "unit_price_3": effective.get("unit_price_3"),
        "best_units": effective.get("best_units"),
        "best_unit_price": effective.get("best_unit_price"),
        "discount_pct": _discount_pct(snapshot, effective),
        "note": effective.get("note"),
        "valid": effective.get("valid", False),
        "warnings": effective.get("warnings") or [],
        # 当前可用（不靠失效券）的可见价与依据状态
        "visible_unit_price_1": visible.get("unit_price_1"),
        "visible_unit_price_2": visible.get("unit_price_2"),
        "visible_unit_price_3": visible.get("unit_price_3"),
        "visible_best_units": visible.get("best_units"),
        "visible_note": visible.get("note"),
        "visible_degraded": bool(visible.get("degraded")),
        "visible_valid": visible.get("valid") if visible else None,
        "basis_status": snapshot.basis_status or "valid",
        "basis_reason": snapshot.basis_reason,
        "basis_checked_at": iso_utc(snapshot.basis_checked_at),
        "coupons": coupons,
        "captured_at": iso_utc(snapshot.created_at),
    }
    if detail:
        data["effective"] = effective
        data["effective_visible"] = visible
        data["page_total_n2"] = round(snapshot.page_price * 2, 2)
        data["page_total_n3"] = round(snapshot.page_price * 3, 2)
    return data


def _coupon_links(db: Session, snapshot: Snapshot, *, detail: bool = False) -> list[dict]:
    from src.constants.enums import CLAIM_STATUS_LABELS, COUPON_STATUS_LABELS
    from src.models.coupon import SnapshotCoupon
    from src.services import coupon_service

    rows = db.scalars(select(SnapshotCoupon).where(SnapshotCoupon.snapshot_id == snapshot.id)).all()
    items = []
    for link in rows:
        items.append(
            {
                "coupon_id": link.coupon_id,
                "kind": link.kind,
                "account": link.capture_account,
                "status_at_capture": link.status_at_capture,
                "status_now": link.status_now,
                "status_now_label": COUPON_STATUS_LABELS.get(link.status_now, link.status_now),
                "claim_status": link.claim_status,
                "claim_status_label": CLAIM_STATUS_LABELS.get(link.claim_status, link.claim_status),
                "claimable": link.claimable,
                "remaining_at_capture": link.remaining_at_capture,
                "remaining_now": link.remaining_now,
                "reason": link.reason,
                "checked_at": iso_utc(link.checked_at),
            }
        )
    if detail:
        from src.models.coupon import Coupon

        codes = {row.id: row for row in db.scalars(select(Coupon)).all()}
        for item in items:
            coupon = codes.get(item["coupon_id"])
            item["code"] = coupon.code if coupon else None
            item["title"] = coupon.title if coupon else None
    return items


def _discount_pct(snapshot: Snapshot, effective: dict) -> float:
    if effective.get("discount_pct") is not None:
        return round(float(effective["discount_pct"]), 2)
    unit1 = effective.get("unit_price_1")
    if not unit1 or not snapshot.page_price:
        return 0.0
    return round((1 - unit1 / snapshot.page_price) * 100, 2)


def list_snapshots(
    db: Session,
    *,
    captured_on: date | None = None,
    competitor_id: int | None = None,
    category: str | None = None,
    sku_id: int | None = None,
    q: str | None = None,
    basis_status: str | None = None,
    role: str = "viewer",
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(Snapshot).join(Sku, Sku.id == Snapshot.sku_id)
    count_stmt = select(func.count()).select_from(Snapshot).join(Sku, Sku.id == Snapshot.sku_id)
    if captured_on:
        stmt = stmt.where(Snapshot.captured_on == captured_on)
        count_stmt = count_stmt.where(Snapshot.captured_on == captured_on)
    if basis_status:
        stmt = stmt.where(Snapshot.basis_status == basis_status)
        count_stmt = count_stmt.where(Snapshot.basis_status == basis_status)
    if competitor_id:
        stmt = stmt.where(Snapshot.competitor_id == competitor_id)
        count_stmt = count_stmt.where(Snapshot.competitor_id == competitor_id)
    if category:
        stmt = stmt.where(Sku.category == category)
        count_stmt = count_stmt.where(Sku.category == category)
    if sku_id:
        stmt = stmt.where(Snapshot.sku_id == sku_id)
        count_stmt = count_stmt.where(Snapshot.sku_id == sku_id)
    if q:
        like = f"%{q.strip()}%"
        condition = or_(Sku.name.ilike(like), Sku.sku_code.ilike(like))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Snapshot.captured_on.desc(), Snapshot.id.desc()).limit(limit).offset(offset)).all()
    return {
        "items": [serialize(db, row, role) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_snapshot(db: Session, snapshot_id: int) -> Snapshot:
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise not_found("snapshot", snapshot_id)
    return snapshot


def capture_days(db: Session) -> list[str]:
    rows = db.execute(select(Snapshot.captured_on).distinct().order_by(Snapshot.captured_on.desc())).all()
    return [row[0].isoformat() for row in rows if row[0]]


def recompute(db: Session, snapshot: Snapshot) -> dict:
    """重算抓价口径 + 按当前券状态重算可见价，并刷新依据状态（不领券、不改名额）。"""
    from src.models.competitor import Competitor as _Competitor

    from src.services import coupon_service

    result = effective_price.compute_effective_price(spec_of(snapshot))
    snapshot.effective = result
    competitor = db.get(_Competitor, snapshot.competitor_id)
    coupon_service.link_coupons(db, snapshot, competitor=competitor)
    coupon_service.refresh_snapshot(db, snapshot, competitor=competitor, do_claim=False)
    return result


def ensure_coupon_links(db: Session, snapshot: Snapshot) -> None:
    """只挂券台账 + 按当前券状态刷新可见价与依据（不领券、不占名额、无副作用）。"""
    from src.models.competitor import Competitor as _Competitor

    from src.services import coupon_service

    competitor = db.get(_Competitor, snapshot.competitor_id)
    coupon_service.link_coupons(db, snapshot, competitor=competitor)
    coupon_service.refresh_snapshot(db, snapshot, competitor=competitor, do_claim=False)


def refresh(db: Session, snapshot: Snapshot) -> dict:
    """页面「核对当前可用价」：核对券状态 + 幂等领取（同日重跑不重复领券）+ 重算可见价。

    只有当天抓价的快照才走领取：核对历史快照只读刷新状态，不会为过去的日子
    额外占用名额，也不会把账号可领次数用在补看旧价上。
    """
    from src.models.competitor import Competitor as _Competitor

    from src.services import coupon_service
    from src.utils.timeutil import local_now

    competitor = db.get(_Competitor, snapshot.competitor_id)
    coupon_service.link_coupons(db, snapshot, competitor=competitor)
    info = coupon_service.refresh_snapshot(
        db,
        snapshot,
        competitor=competitor,
        captured_on=snapshot.captured_on,
        do_claim=snapshot.captured_on == local_now().date(),
    )
    if not (snapshot.effective or {}).get("valid"):
        snapshot.effective = effective_price.compute_effective_price(spec_of(snapshot))
    return info

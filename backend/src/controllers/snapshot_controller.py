"""抓价快照控制器：列表/详情/补录。"""
from __future__ import annotations

from datetime import date

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.snapshot import Snapshot
from src.services import audit_service, effective_price, snapshot_service
from src.utils.errors import AppError
from src.utils.timeutil import local_now


def list_snapshots(db: Session, **filters) -> dict:
    data = snapshot_service.list_snapshots(db, **filters)
    data["capture_days"] = snapshot_service.capture_days(db)
    data["invalidated_total"] = db.scalar(
        select(func.count()).select_from(Snapshot).where(Snapshot.basis_status == "invalidated")
    ) or 0
    return data


def get_snapshot(db: Session, snapshot_id: int, role: str) -> dict:
    snapshot = snapshot_service.get_snapshot(db, snapshot_id)
    if not (snapshot.effective or {}).get("valid"):
        snapshot_service.recompute(db, snapshot)
        db.commit()
    elif not (snapshot.effective_visible or {}).get("valid"):
        # 历史快照/补录快照首次打开时补一次券台账关联，只读刷新，不领券
        snapshot_service.ensure_coupon_links(db, snapshot)
        db.commit()
    return snapshot_service.serialize(db, snapshot, role, detail=True)


def refresh_snapshot(db: Session, snapshot_id: int, payload: dict, principal: dict, request: Request) -> dict:
    """核对当前可用价：刷新券状态/名额/领取权（幂等领券），券不可领就重算可见价并标依据失效。"""
    snapshot = snapshot_service.get_snapshot(db, snapshot_id)
    info = snapshot_service.refresh(db, snapshot)
    rerun = None
    is_today = snapshot.captured_on == local_now().date()
    if info["invalidated"] and payload.get("rerun", True) and is_today:
        # 依据失效就当场重算：旧待审批建议作废，新建议按当前可见价生成
        from src.services import pricing_service

        captured_on = snapshot.captured_on
        run = pricing_service.compute_run(
            db,
            captured_on=captured_on,
            operator_email=principal["email"],
            trigger="manual",
        )
        rerun = {
            "run_no": run.run_no,
            "run_id": run.id,
            "status": run.status,
            "advices": run.output_count,
            "invalidated_snapshots": (run.metrics or {}).get("invalidated_snapshots", 0),
            "voided_advices": (run.metrics or {}).get("voided_advices", 0),
            "claims_new": (run.metrics or {}).get("claims_new", 0),
            "claims_reused": (run.metrics or {}).get("claims_reused", 0),
        }
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="run.create",
            entity="run",
            entity_id=run.id,
            detail={"trigger": "coupon_check", "run_no": run.run_no, "captured_on": str(captured_on)},
            ip=request.client.host if request.client else None,
        )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="snapshot.coupon_check",
        entity="snapshot",
        entity_id=snapshot.id,
        detail={
            "captured_on": snapshot.captured_on.isoformat(),
            "claim": info["claims"],
            "invalidated": info["invalidated"],
            "unavailable": [item["kind"] for item in info["unavailable"]],
            "reason": payload.get("reason"),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    data = snapshot_service.serialize(db, snapshot, principal["role"], detail=True)
    data["coupon_check"] = {
        "claims": info["claims"],
        "invalidated": info["invalidated"],
        "unavailable": info["unavailable"],
        "visible_unit_price_1": data["visible_unit_price_1"],
        "basis_status": data["basis_status"],
        "basis_reason": data["basis_reason"],
        "rerun": rerun,
    }
    return data


def create_snapshot(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    existing = db.scalar(
        select(Snapshot).where(Snapshot.sku_id == payload["sku_id"], Snapshot.captured_on == payload["captured_on"])
    )
    if existing is not None:
        raise AppError("CONFLICT", 409, {"sku_id": payload["sku_id"], "captured_on": str(payload["captured_on"])})
    from src.models.sku import Sku

    sku = db.get(Sku, payload["sku_id"])
    if sku is None:
        raise AppError("NOT_FOUND", 404, {"entity": "sku", "id": payload["sku_id"]})
    spec = {
        "page_price": payload["page_price"],
        "full_reduction": payload.get("full_reduction") or [],
        "shop_coupon": payload.get("shop_coupon"),
        "platform_coupon": payload.get("platform_coupon"),
        "member_discount": payload.get("member_discount", 1.0),
    }
    result = effective_price.compute_effective_price(spec)
    snapshot = Snapshot(
        sku_id=sku.id,
        competitor_id=sku.competitor_id,
        captured_on=payload["captured_on"],
        page_price=payload["page_price"],
        full_reduction=payload.get("full_reduction") or [],
        shop_coupon=payload.get("shop_coupon"),
        platform_coupon=payload.get("platform_coupon"),
        member_discount=payload.get("member_discount", 1.0),
        stock=payload.get("stock") or 0,
        campaign=payload.get("campaign") or "手工补录",
        effective=result,
    )
    db.add(snapshot)
    db.flush()
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="snapshot.capture",
        entity="snapshot",
        entity_id=snapshot.id,
        detail={
            "sku_code": sku.sku_code,
            "captured_on": payload["captured_on"].isoformat() if isinstance(payload["captured_on"], date) else str(payload["captured_on"]),
            "unit_price_1": result.get("unit_price_1"),
            "valid": result.get("valid"),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return snapshot_service.serialize(db, snapshot, principal["role"], detail=True)

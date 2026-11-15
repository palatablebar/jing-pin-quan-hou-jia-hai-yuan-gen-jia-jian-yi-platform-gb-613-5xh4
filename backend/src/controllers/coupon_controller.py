"""券台账控制器：列表/详情/状态调整（名额、下线、资格）、抓价账号领取权核对。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, coupon_service
from src.utils.errors import not_found


def list_coupons(db: Session, **filters) -> dict:
    data = coupon_service.list_coupons(db, **filters)
    data["status_options"] = [
        {"value": "active", "label": "可领"},
        {"value": "exhausted", "label": "已抢空"},
        {"value": "paused", "label": "已下线"},
        {"value": "expired", "label": "已过期"},
    ]
    return data


def get_coupon(db: Session, coupon_id: int) -> dict:
    from src.models.coupon import Coupon

    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise not_found("coupon", coupon_id)
    return coupon_service.detail(db, coupon)


def update_coupon(db: Session, coupon_id: int, payload: dict, principal: dict, request: Request) -> dict:
    from src.models.coupon import Coupon

    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise not_found("coupon", coupon_id)
    changes = {key: value for key, value in payload.items() if value is not None}
    result = coupon_service.apply_state_change(
        db,
        coupon,
        changes,
        reason=payload.get("reason"),
        actor_email=principal["email"],
        actor_role=principal["role"],
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="coupon.update",
        entity="coupon",
        entity_id=coupon.id,
        detail={
            "code": coupon.code,
            "fields": result["changed_fields"],
            "before": result["before"],
            "after": {"status": coupon.status, "remaining": coupon.remaining, "eligible": coupon.eligible},
            "reason": payload.get("reason"),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return coupon_service.detail(db, coupon)

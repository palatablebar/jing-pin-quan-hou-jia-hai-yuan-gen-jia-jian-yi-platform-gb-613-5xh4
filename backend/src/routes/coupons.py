from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import COUPON_WRITE_ROLES
from src.controllers import coupon_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.coupon import CouponPatch
from src.utils.pagination import page_params

router = APIRouter(prefix="/coupons", tags=["coupons"])


@router.get("")
def list_coupons(
    status: str | None = None,
    competitor_id: int | None = None,
    sku_id: int | None = None,
    q: str | None = None,
    limit: int = Query(50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return coupon_controller.list_coupons(
        db,
        status=status,
        competitor_id=competitor_id,
        sku_id=sku_id,
        q=q,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{coupon_id}")
def get_coupon(coupon_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    return coupon_controller.get_coupon(db, coupon_id)


@router.patch("/{coupon_id}")
def update_coupon(
    coupon_id: int,
    payload: CouponPatch,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*COUPON_WRITE_ROLES)),
) -> dict:
    return coupon_controller.update_coupon(db, coupon_id, payload.model_dump(exclude_unset=True), principal, request)

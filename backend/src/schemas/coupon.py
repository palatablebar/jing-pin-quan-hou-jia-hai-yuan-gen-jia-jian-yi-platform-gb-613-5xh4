from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class CouponPatch(BaseModel):
    """调整券状态 / 名额 / 领取资格。名额与状态变化会留痕（coupon_events）。"""

    status: str | None = Field(default=None, pattern=r"^(active|exhausted|paused|expired)$")
    total_quota: int | None = Field(default=None, ge=0, le=1_000_000)
    claimed_count: int | None = Field(default=None, ge=0, le=1_000_000)
    per_account_limit: int | None = Field(default=None, ge=1, le=100)
    eligible: bool | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    title: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=500)


class CouponCheckIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    rerun: bool = True  # 发现依据失效时，立即按当前可见价重算当天测算

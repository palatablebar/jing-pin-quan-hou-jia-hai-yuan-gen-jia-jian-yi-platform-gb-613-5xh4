"""限量券台账：券面配置、名额、抓价账号领取权与状态变更留痕。

设计要点：
  Coupon        当前券状态 + 总名额 + 已领数（名额变化的「现在」）
  SnapshotCoupon 某条抓价快照引用了哪张券、抓价时与当前的状态（每次券状态的快照）
  CouponClaim   抓价账号的领取记录；claim_key 唯一，重跑同一次抓价只累加 attempts、不重复占名额
  CouponEvent   状态 / 名额 / 资格每次变化的前后值（谁改的、为什么）

券面金额的结算口径仍以抓价快照里的 shop_coupon / platform_coupon 为准；本表只负责
「这张券现在还能不能领、账号还有没有资格、名额还剩多少」。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (UniqueConstraint("code", name="uq_coupons_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), index=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    kind: Mapped[str] = mapped_column(String(24), default="shop_coupon")  # shop_coupon / platform_coupon
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    minus: Mapped[float] = mapped_column(Float, default=0.0)
    per: Mapped[float] = mapped_column(Float, default=0.0)  # 平台每满券的「每」
    stackable: Mapped[bool] = mapped_column(Boolean, default=True)
    total_quota: Mapped[int] = mapped_column(Integer, default=0)
    claimed_count: Mapped[int] = mapped_column(Integer, default=0)
    per_account_limit: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/exhausted/paused/expired
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)  # 抓价账号是否具备领取资格（会员/新客等）
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def remaining(self) -> int:
        return max(int(self.total_quota or 0) - int(self.claimed_count or 0), 0)


class SnapshotCoupon(Base):
    """快照与券的关联：抓价时状态 + 最近一次核对后的状态（两条都保留）。"""

    __tablename__ = "snapshot_coupons"
    __table_args__ = (UniqueConstraint("snapshot_id", "coupon_id", name="uq_snapshot_coupons"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="shop_coupon")
    capture_account: Mapped[str] = mapped_column(String(160), default="")
    status_at_capture: Mapped[str] = mapped_column(String(16), default="active")
    status_now: Mapped[str] = mapped_column(String(16), default="active")
    claim_status_at_capture: Mapped[str] = mapped_column(String(24), default="not_claimed")
    claim_status: Mapped[str] = mapped_column(String(24), default="not_claimed")
    claimable: Mapped[bool] = mapped_column(Boolean, default=True)
    remaining_at_capture: Mapped[int] = mapped_column(Integer, default=0)
    remaining_now: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CouponClaim(Base):
    """抓价账号领取记录。claim_key = 券 + 账号 + SKU + 抓价日，唯一约束保证重试幂等。"""

    __tablename__ = "coupon_claims"
    __table_args__ = (UniqueConstraint("claim_key", name="uq_coupon_claims_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="CASCADE"), index=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id", ondelete="SET NULL"), nullable=True, index=True)
    account_email: Mapped[str] = mapped_column(String(160), index=True)
    captured_on: Mapped[date] = mapped_column(Date, index=True)
    claim_key: Mapped[str] = mapped_column(String(200), unique=True)
    result: Mapped[str] = mapped_column(String(24), default="claimed")  # claimed/exhausted/ineligible/unavailable
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    first_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)


class CouponEvent(Base):
    """券状态 / 名额 / 资格变更留痕（含前后值）。"""

    __tablename__ = "coupon_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("coupons.id", ondelete="CASCADE"), index=True)
    event: Mapped[str] = mapped_column(String(32), default="update", index=True)
    before_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    before_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    after_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_email: Mapped[str] = mapped_column(String(160), default="system")
    actor_role: Mapped[str] = mapped_column(String(20), default="system")
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

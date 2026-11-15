"""限量券台账服务：券状态 / 名额 / 抓价账号领取权 / 依据失效重算。

职责边界：
  - 结算金额仍由算法 A 根据快照里的券面配置计算（本模块不重写金额口径）；
  - 本模块判断「这张券现在还能不能领」，把不可领的券从可见价口径里剔除，
    并把依赖该券的跟价建议标记为「依据失效」，等待重算。

幂等：领取记录以 (券, 账号, 抓价日) 为 claim_key；同一抓价重跑只累加 attempts，
      不重复占名额、不重复消耗账号可领次数。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import false, func, select
from sqlalchemy.orm import Session

from src.constants.enums import ClaimStatus, CouponStatus
from src.models.competitor import Competitor
from src.models.coupon import Coupon, CouponClaim, CouponEvent, SnapshotCoupon
from src.models.snapshot import Snapshot
from src.services import effective_price
from src.utils.timeutil import local_now, utcnow

CLAIMABLE = (ClaimStatus.CLAIMED.value, ClaimStatus.REUSED.value)
FAILED_CLAIMS = (
    ClaimStatus.LIMIT_REACHED.value,
    ClaimStatus.INELIGIBLE.value,
    ClaimStatus.EXHAUSTED.value,
    ClaimStatus.UNAVAILABLE.value,
)


def account_of(competitor: Competitor | None) -> str:
    """抓价账号：优先取店铺 capture_config.capture_account，否则按竞品编码派生。"""
    if competitor is None:
        return "rpa@pricewatch.io"
    config = competitor.capture_config or {}
    account = str(config.get("capture_account") or "").strip()
    return account or f"rpa@{competitor.code}.pricewatch.io"


def claim_key(coupon: Coupon, account: str, sku_id: int | None, captured_on: date) -> str:
    """幂等键只到「券 + 账号 + 抓价日」：同一份抓价重跑多少次都只算一次领取。"""
    return f"{coupon.code}|{account}|{captured_on.isoformat()}"


def effective_status(coupon: Coupon, on_date: date | None = None) -> str:
    """券当前状态：下线 / 过期（含有效期外） / 抢空 / 可领。"""
    if coupon.status == CouponStatus.PAUSED.value:
        return CouponStatus.PAUSED.value
    if coupon.status == CouponStatus.EXPIRED.value:
        return CouponStatus.EXPIRED.value
    day = on_date or date.today()
    # 有效期按「今天是否可领」判断：只有明确过了到期日才算过期（生效日尚未到时仍按抓价日口径核对）
    if coupon.valid_until and day > coupon.valid_until:
        return CouponStatus.EXPIRED.value
    if coupon.remaining <= 0:
        return CouponStatus.EXHAUSTED.value
    return CouponStatus.ACTIVE.value


STATUS_REASONS = {
    CouponStatus.PAUSED.value: "券已下线，当前不可领取",
    CouponStatus.EXPIRED.value: "券已过期或不在有效期，当前不可领取",
    CouponStatus.EXHAUSTED.value: "券名额已抢空",
}


def evaluate(coupon: Coupon, on_date: date | None = None) -> tuple[str | None, str | None]:
    """只按券自身状态判断可领性，返回 (失败原因码, 说明)；可领时返回 (None, None)。"""
    status = effective_status(coupon, on_date)
    if status != CouponStatus.ACTIVE.value:
        reason = STATUS_REASONS[status]
        if status == CouponStatus.EXHAUSTED.value:
            reason = f"券名额已抢空（已领 {coupon.claimed_count}/{coupon.total_quota}）"
        return {
            CouponStatus.PAUSED.value: ClaimStatus.UNAVAILABLE.value,
            CouponStatus.EXPIRED.value: ClaimStatus.UNAVAILABLE.value,
            CouponStatus.EXHAUSTED.value: ClaimStatus.EXHAUSTED.value,
        }[status], reason
    if not coupon.eligible:
        return ClaimStatus.INELIGIBLE.value, "抓价账号不满足领取资格（新客 / 会员限制）"
    return None, None


def _account_claimed(db: Session, coupon: Coupon, account: str) -> int:
    """该账号在这张券上累计成功领取的次数（per_account_limit 是账号领取权总次数）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(CouponClaim)
            .where(
                CouponClaim.coupon_id == coupon.id,
                CouponClaim.account_email == account,
                CouponClaim.result == ClaimStatus.CLAIMED.value,
            )
        )
        or 0
    )


def record_event(
    db: Session,
    coupon: Coupon,
    *,
    event: str,
    reason: str | None = None,
    actor_email: str = "system",
    actor_role: str = "system",
    snapshot_id: int | None = None,
    before: dict | None = None,
    seed_key: str | None = None,
) -> CouponEvent:
    before = before or {}
    log = CouponEvent(
        coupon_id=coupon.id,
        event=event,
        before_status=before.get("status"),
        after_status=coupon.status,
        before_remaining=before.get("remaining"),
        after_remaining=coupon.remaining,
        before_eligible=before.get("eligible"),
        after_eligible=coupon.eligible,
        reason=reason,
        actor_email=actor_email,
        actor_role=actor_role,
        snapshot_id=snapshot_id,
        seed_key=seed_key,
    )
    db.add(log)
    return log


def _already_recorded(db: Session, coupon: Coupon, status: str | None, remaining: int | None) -> bool:
    """同一券「当前状态 + 当前名额」已经记过 observed_change 就不再重复记。"""
    latest = db.scalar(
        select(CouponEvent)
        .where(CouponEvent.coupon_id == coupon.id, CouponEvent.event == "observed_change")
        .order_by(CouponEvent.id.desc())
    )
    return latest is not None and latest.after_status == status and latest.after_remaining == remaining


def resolve_claim(
    db: Session,
    coupon: Coupon,
    *,
    account: str,
    sku_id: int | None,
    captured_on: date,
    now: datetime | None = None,
) -> tuple[CouponClaim, str, str | None, bool]:
    """领取一次券（幂等）。返回 (领取记录, 本次领取状态, 原因, 是否本次真正占用名额)。

    顺序很重要：
    1. 券当前已不可领（抢空 / 下线 / 过期 / 无资格）→ 直接返回失败原因，
       绝不再发起领取，也绝不用一次失败的尝试覆盖「早班已领到」的历史事实；
    2. 本账号当天已成功领过 → 复用，不再领第二次（不重复占名额、不耗可领次数）；
    3. 否则按 per_account_limit 判断账号领取权，能领才真正占一个名额。
    """
    now = now or utcnow()
    key = claim_key(coupon, account, sku_id, captured_on)
    existing = db.scalar(select(CouponClaim).where(CouponClaim.claim_key == key))
    if existing is None:
        existing = CouponClaim(
            coupon_id=coupon.id,
            sku_id=sku_id,
            account_email=account,
            captured_on=captured_on,
            claim_key=key,
            result=ClaimStatus.NOT_CLAIMED.value,
            first_attempt_at=now,
            last_attempt_at=now,
            attempts=0,
        )
        db.add(existing)
        db.flush()
    existing.attempts += 1
    existing.last_attempt_at = now

    unavailable_code, unavailable_reason = evaluate(coupon, captured_on)
    if unavailable_code is not None:
        return existing, unavailable_code, unavailable_reason, False
    if existing.result == ClaimStatus.CLAIMED.value:
        return existing, ClaimStatus.REUSED.value, "复用抓价账号当日已领记录，不重复占名额", False

    used = _account_claimed(db, coupon, account)
    limit = int(coupon.per_account_limit or 1)
    if used + 1 > limit:
        reason = f"抓价账号可领次数已用尽（{used}/{limit}）"
        existing.result = ClaimStatus.LIMIT_REACHED.value
        existing.reason = reason
        return existing, ClaimStatus.LIMIT_REACHED.value, reason, False

    before = {"status": coupon.status, "remaining": coupon.remaining, "eligible": coupon.eligible}
    existing.result = ClaimStatus.CLAIMED.value
    existing.reason = None
    coupon.claimed_count = min(int(coupon.total_quota or 0), int(coupon.claimed_count or 0) + 1)
    record_event(
        db,
        coupon,
        event="claim",
        reason=f"{account} 领取 1 张",
        actor_email=account,
        actor_role="rpa",
        before=before,
    )
    if coupon.remaining == 0:
        record_event(db, coupon, event="exhausted", reason="抓价账号领取后名额归零", before=before)
    return existing, ClaimStatus.CLAIMED.value, None, True


def link_coupons(db: Session, snapshot: Snapshot, *, competitor: Competitor | None = None, now: datetime | None = None) -> list[SnapshotCoupon]:
    """把快照里配置的券挂到券台账（幂等）。返回该快照的券关联列表。"""
    now = now or utcnow()
    account = account_of(competitor)
    kinds = []
    if snapshot.shop_coupon:
        kinds.append("shop_coupon")
    if snapshot.platform_coupon:
        kinds.append("platform_coupon")
    if not kinds:
        return []
    existing = {row.kind: row for row in db.scalars(select(SnapshotCoupon).where(SnapshotCoupon.snapshot_id == snapshot.id)).all()}
    for kind in kinds:
        if kind in existing:
            continue
        coupon = db.scalar(
            select(Coupon)
            .where(
                Coupon.competitor_id == snapshot.competitor_id,
                Coupon.kind == kind,
                (Coupon.sku_id == snapshot.sku_id) | (Coupon.sku_id.is_(None)),
            )
            .order_by(Coupon.sku_id.is_(None), Coupon.id)
        )
        if coupon is None:
            continue
        status_now = effective_status(coupon, snapshot.captured_on)
        reason = None if status_now == CouponStatus.ACTIVE.value and coupon.eligible else _status_reason(coupon, status_now)
        link = SnapshotCoupon(
            snapshot_id=snapshot.id,
            coupon_id=coupon.id,
            kind=kind,
            capture_account=account,
            status_at_capture=status_now,
            status_now=status_now,
            claim_status=ClaimStatus.NOT_CLAIMED.value,
            claimable=reason is None,
            remaining_at_capture=coupon.remaining,
            remaining_now=coupon.remaining,
            reason=reason,
            captured_at=now,
            checked_at=now,
        )
        db.add(link)
        existing[kind] = link
    db.flush()
    return list(existing.values())


def _status_reason(coupon: Coupon, status: str) -> str:
    if status == CouponStatus.EXHAUSTED.value:
        return f"券名额已抢空（已领 {coupon.claimed_count}/{coupon.total_quota}）"
    if status == CouponStatus.ACTIVE.value and not coupon.eligible:
        return "抓价账号不满足领取资格（新客 / 会员限制）"
    return STATUS_REASONS.get(status, "券当前不可领取")


def refresh_snapshot(
    db: Session,
    snapshot: Snapshot,
    *,
    competitor: Competitor | None,
    captured_on: date | None = None,
    do_claim: bool = False,
    now: datetime | None = None,
    on_date: date | None = None,
) -> dict:
    """核对快照引用的券：更新状态/名额/领取权，重算可见价，标记依据是否失效。

    do_claim=True 且快照属于本次抓价日时，会走幂等领取（重跑不重复领券）。
    on_date 是「现在」的业务日期，用于判断券是否过期；默认按抓价日核对历史口径。
    """
    now = now or utcnow()
    account = account_of(competitor)
    check_date = on_date or local_now(now).date()
    links = list(db.scalars(select(SnapshotCoupon).where(SnapshotCoupon.snapshot_id == snapshot.id)).all())
    unavailable: dict[str, str] = {}
    unavailable_details: list[dict] = []
    claim_stats = {"claimed": 0, "reused": 0, "blocked": 0}
    for link in links:
        coupon = db.get(Coupon, link.coupon_id)
        if coupon is None:
            continue
        reason: str | None = None
        if do_claim and captured_on and snapshot.captured_on == captured_on:
            claim, claim_status, reason, consumed = resolve_claim(
                db, coupon, account=account, sku_id=snapshot.sku_id, captured_on=snapshot.captured_on, now=now
            )
            link.claim_status = claim_status
            link.capture_account = account
            if claim_status == ClaimStatus.REUSED.value:
                # 早班已领到、本次不再领取：券对本次抓价仍然可用，不算失败
                reason = None
                claim_stats["reused"] += 1
            elif claim_status == ClaimStatus.CLAIMED.value:
                claim_stats["claimed"] += 1
            else:
                claim_stats["blocked"] += 1
        else:
            code, reason = evaluate(coupon, check_date)
            latest = db.scalar(
                select(CouponClaim)
                .where(
                    CouponClaim.coupon_id == coupon.id,
                    CouponClaim.account_email == account,
                    CouponClaim.captured_on == snapshot.captured_on,
                )
                .order_by(CouponClaim.id.desc())
            )
            if code is not None:
                link.claim_status = code
            else:
                # 券现在可领：仍展示账号当天的实际领取结果（领过就是领过，没领过才是未领取）
                link.claim_status = latest.result if latest is not None else ClaimStatus.NOT_CLAIMED.value
        # 可领 = 券现在可领，且本次没有被拦下（reused 只是「早班已领、本次不用再领」，不是失败）
        blocked = link.claim_status in FAILED_CLAIMS
        link.claimable = (not blocked) and reason is None
        if link.claim_status_at_capture in (None, "", ClaimStatus.NOT_CLAIMED.value) and link.claim_status in CLAIMABLE:
            link.claim_status_at_capture = link.claim_status
        link.status_now = effective_status(coupon, check_date)
        link.remaining_now = coupon.remaining
        link.status_at_capture = link.status_at_capture or link.status_now
        # 被动变化（抢空 / 到期 / 资格失效）也要留痕：同一次变化只记一条，避免按快照数刷屏
        if link.status_now != link.status_at_capture or link.remaining_now != link.remaining_at_capture:
            if not _already_recorded(db, coupon, link.status_now, link.remaining_now):
                record_event(
                    db,
                    coupon,
                    event="observed_change",
                    reason=f"核对快照 #{snapshot.id} 时发现券状态变化",
                    actor_email=account,
                    actor_role="rpa",
                    snapshot_id=snapshot.id,
                    before={"status": link.status_at_capture, "remaining": link.remaining_at_capture},
                )
            link.status_at_capture = link.status_now
            link.remaining_at_capture = link.remaining_now
        link.checked_at = now
        if link.claimable:
            link.reason = None
        else:
            link.reason = reason or _status_reason(coupon, link.status_now)
            unavailable[link.kind] = link.reason
            unavailable_details.append(
                {
                    "coupon_id": coupon.id,
                    "code": coupon.code,
                    "kind": link.kind,
                    "title": coupon.title,
                    "status": link.status_now,
                    "claim_status": link.claim_status,
                    "remaining": link.remaining_now,
                    "reason": link.reason,
                }
            )
    spec = {
        "page_price": snapshot.page_price,
        "full_reduction": snapshot.full_reduction or [],
        "shop_coupon": snapshot.shop_coupon,
        "platform_coupon": snapshot.platform_coupon,
        "member_discount": snapshot.member_discount,
    }
    visible = effective_price.compute_effective_price(spec, unavailable=unavailable)
    snapshot.effective_visible = visible
    snapshot.basis_checked_at = now
    if unavailable_details:
        snapshot.basis_status = "invalidated"
        snapshot.basis_reason = "；".join(
            f"{item['title'] or item['code']}：{item['reason']}" for item in unavailable_details
        )
    else:
        snapshot.basis_status = "valid"
        snapshot.basis_reason = None
    return {
        "visible": visible,
        "unavailable": unavailable_details,
        "claims": claim_stats,
        "invalidated": bool(unavailable_details),
    }


def visible_unit_price(snapshot: Snapshot) -> float | None:
    """当前可用（不靠失效券）的券后 1 件价；没有可用结论时回落到抓价口径。"""
    visible = snapshot.effective_visible or {}
    if visible.get("valid") and visible.get("unit_price_1") is not None:
        return float(visible["unit_price_1"])
    effective = snapshot.effective or {}
    if effective.get("unit_price_1") is not None:
        return float(effective["unit_price_1"])
    return None


def list_coupons(
    db: Session,
    *,
    status: str | None = None,
    competitor_id: int | None = None,
    sku_id: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    from src.models.sku import Sku

    stmt = select(Coupon)
    if competitor_id:
        stmt = stmt.where(Coupon.competitor_id == competitor_id)
    if sku_id:
        stmt = stmt.where(Coupon.sku_id == sku_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where((Coupon.code.ilike(like)) | (Coupon.title.ilike(like)))
    rows = db.scalars(stmt.order_by(Coupon.id)).all()
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    items = [serialize(db, row, competitors.get(row.competitor_id), skus.get(row.sku_id)) for row in rows]
    if status:
        # 状态是「按今天的名额 / 有效期」推导出来的，先过滤再分页，避免分页口径错位
        items = [item for item in items if item["status"] == status]
    total = len(items)
    return {"items": items[offset : offset + limit], "total": total, "limit": limit, "offset": offset}


def serialize(db: Session, coupon: Coupon, competitor: Competitor | None = None, sku=None) -> dict:
    from src.constants.enums import CLAIM_STATUS_LABELS, COUPON_KIND_LABELS, COUPON_STATUS_LABELS

    status = effective_status(coupon)
    link_count = db.scalar(select(func.count()).select_from(SnapshotCoupon).where(SnapshotCoupon.coupon_id == coupon.id)) or 0
    claimed_links = (
        db.scalar(
            select(func.count())
            .select_from(SnapshotCoupon)
            .where(SnapshotCoupon.coupon_id == coupon.id, SnapshotCoupon.claim_status.in_(CLAIMABLE))
        )
        or 0
    )
    return {
        "id": coupon.id,
        "code": coupon.code,
        "title": coupon.title,
        "kind": coupon.kind,
        "kind_label": COUPON_KIND_LABELS.get(coupon.kind, coupon.kind),
        "competitor_id": coupon.competitor_id,
        "competitor_name": competitor.name if competitor else None,
        "sku_id": coupon.sku_id,
        "sku_code": sku.sku_code if sku else None,
        "sku_name": sku.name if sku else None,
        "threshold": coupon.threshold,
        "minus": coupon.minus,
        "per": coupon.per,
        "stackable": coupon.stackable,
        "total_quota": coupon.total_quota,
        "claimed_count": coupon.claimed_count,
        "remaining": coupon.remaining,
        "per_account_limit": coupon.per_account_limit,
        "status": status,
        "status_label": COUPON_STATUS_LABELS.get(status, status),
        "eligible": coupon.eligible,
        "claim_status": ClaimStatus.NOT_CLAIMED.value,
        "claim_status_label": CLAIM_STATUS_LABELS[ClaimStatus.NOT_CLAIMED.value],
        "valid_from": coupon.valid_from.isoformat() if coupon.valid_from else None,
        "valid_until": coupon.valid_until.isoformat() if coupon.valid_until else None,
        "note": coupon.note,
        "snapshot_links": link_count,
        "claimed_links": claimed_links,
        "updated_at": coupon.updated_at.isoformat(timespec="seconds") + "Z" if coupon.updated_at else None,
    }


def detail(db: Session, coupon: Coupon) -> dict:
    from src.constants.enums import CLAIM_STATUS_LABELS, COUPON_STATUS_LABELS
    from src.models.advice import Advice
    from src.models.run import Run
    from src.models.sku import Sku

    competitor = db.get(Competitor, coupon.competitor_id)
    sku = db.get(Sku, coupon.sku_id) if coupon.sku_id else None
    data = serialize(db, coupon, competitor, sku)
    links = db.scalars(
        select(SnapshotCoupon).where(SnapshotCoupon.coupon_id == coupon.id).order_by(SnapshotCoupon.id.desc()).limit(50)
    ).all()
    snapshots = {row.id: row for row in db.scalars(select(Snapshot)).all()}
    data["snapshots"] = [
        {
            "snapshot_id": link.snapshot_id,
            "captured_on": snapshots[link.snapshot_id].captured_on.isoformat()
            if link.snapshot_id in snapshots
            else None,
            "status_at_capture": link.status_at_capture,
            "status_now": link.status_now,
            "claim_status": link.claim_status,
            "claim_status_label": CLAIM_STATUS_LABELS.get(link.claim_status, link.claim_status),
            "claimable": link.claimable,
            "remaining_at_capture": link.remaining_at_capture,
            "remaining_now": link.remaining_now,
            "account": link.capture_account,
            "reason": link.reason,
            "checked_at": link.checked_at.isoformat(timespec="seconds") + "Z" if link.checked_at else None,
        }
        for link in links
    ]
    claims = db.scalars(
        select(CouponClaim).where(CouponClaim.coupon_id == coupon.id).order_by(CouponClaim.last_attempt_at.desc()).limit(50)
    ).all()
    data["claims"] = [
        {
            "account": claim.account_email,
            "captured_on": claim.captured_on.isoformat(),
            "result": claim.result,
            "result_label": CLAIM_STATUS_LABELS.get(claim.result, claim.result),
            "reason": claim.reason,
            "attempts": claim.attempts,
            "last_attempt_at": claim.last_attempt_at.isoformat(timespec="seconds") + "Z",
        }
        for claim in claims
    ]
    events = db.scalars(
        select(CouponEvent).where(CouponEvent.coupon_id == coupon.id).order_by(CouponEvent.id.desc()).limit(50)
    ).all()
    data["events"] = [
        {
            "event": event.event,
            "before_status": event.before_status,
            "after_status": event.after_status,
            "before_remaining": event.before_remaining,
            "after_remaining": event.after_remaining,
            "before_eligible": event.before_eligible,
            "after_eligible": event.after_eligible,
            "reason": event.reason,
            "actor": event.actor_email,
            "created_at": event.created_at.isoformat(timespec="seconds") + "Z",
        }
        for event in events
    ]
    advice_rows = db.execute(
        select(Advice, Run, Sku)
        .join(Run, Run.id == Advice.run_id)
        .join(Sku, Sku.id == Advice.sku_id, isouter=True)
        .where(Advice.snapshot_id.in_([link.snapshot_id for link in links]) if links else false())
        .order_by(Advice.id.desc())
        .limit(50)
    ).all()
    data["advices"] = [
        {
            "advice_id": advice.id,
            "run_no": run.run_no,
            "sku_code": sku_row.sku_code if sku_row else None,
            "sku_name": sku_row.name if sku_row else None,
            "status": advice.status,
            "basis_status": advice.basis_status,
            "suggested_price": round(advice.suggested_price, 2),
            "superseded_by_id": advice.superseded_by_id,
        }
        for advice, run, sku_row in advice_rows
    ]
    data["status_label"] = COUPON_STATUS_LABELS.get(data["status"], data["status"])
    data["events_total"] = db.scalar(select(func.count()).select_from(CouponEvent).where(CouponEvent.coupon_id == coupon.id)) or 0
    data["claims_total"] = db.scalar(select(func.count()).select_from(CouponClaim).where(CouponClaim.coupon_id == coupon.id)) or 0
    return data


def apply_state_change(
    db: Session,
    coupon: Coupon,
    changes: dict,
    *,
    reason: str | None,
    actor_email: str,
    actor_role: str,
) -> dict:
    """管理员调整券状态 / 名额 / 资格：留痕前后值，返回变更前的快照值。"""
    before = {"status": coupon.status, "remaining": coupon.remaining, "eligible": coupon.eligible}
    changed_fields = []
    for field in ("status", "total_quota", "claimed_count", "per_account_limit", "eligible", "valid_from", "valid_until", "note", "title"):
        if field in changes and changes[field] is not None and getattr(coupon, field) != changes[field]:
            setattr(coupon, field, changes[field])
            changed_fields.append(field)
    if "status" in changed_fields and coupon.status == CouponStatus.ACTIVE.value and coupon.remaining <= 0:
        # 恢复可领但名额仍为 0：维持抢空语义，避免出现「可领但没名额」的自相矛盾
        coupon.status = CouponStatus.EXHAUSTED.value
    db.flush()
    record_event(
        db,
        coupon,
        event="update",
        reason=reason or "手工调整券状态",
        actor_email=actor_email,
        actor_role=actor_role,
        before=before,
    )
    return {"before": before, "changed_fields": changed_fields}


def basis_signature(snapshot: Snapshot) -> str:
    """依据指纹：券后口径 + 当前可见价，任一变化即说明旧建议的依据不再成立。"""
    visible = snapshot.effective_visible or {}
    unavailable = sorted((visible.get("excluded") or {}).keys())
    return "|".join(
        [
            snapshot.basis_status or "valid",
            ",".join(unavailable),
            str(visible.get("unit_price_1")),
            str(visible.get("best_units")),
        ]
    )


def void_stale_advices(
    db: Session,
    *,
    captured_on: date,
    run_id: int,
    supersede: dict[int, int | None],
    resolution_by_sku: dict[int, str] | None = None,
) -> list[dict]:
    """把旧测算里「依据已变」的待审批建议作废 / 标记，并记录建议去向。

    - 依据指纹变了（券抢空 / 下线 / 资格变化导致可见价变化）且仍待审批 → 置为 voided；
    - 已批准 / 已驳回是当时依据下的历史结论，不改状态也不改依据标记，便于复盘；
    - 已作废的建议重复扫描时补记去向（supersede），保证页面能看到「去了哪条新建议」。
    supersede: {sku_id: 新建议 id 或 None}
    """
    from src.models.advice import Advice
    from src.models.run import Run

    resolution_by_sku = resolution_by_sku or {}
    rows = db.execute(
        select(Advice, Run)
        .join(Run, Run.id == Advice.run_id)
        .where(Advice.run_id != run_id, Advice.status.in_(["pending", "voided"]))
    ).all()
    changed: list[dict] = []
    for advice, run in rows:
        snapshot = db.get(Snapshot, advice.snapshot_id) if advice.snapshot_id else None
        if snapshot is None:
            continue
        payload = dict(advice.payload or {})
        stored_signature = payload.get("basis_signature")
        if stored_signature is None:
            continue  # 老建议没有依据指纹（升级前的数据），不擅自作废，只在重跑后补上
        if stored_signature == basis_signature(snapshot):
            continue  # 依据没变，旧结论仍然有效
        payload["superseded_by"] = supersede.get(advice.sku_id)
        payload["resolution"] = resolution_by_sku.get(advice.sku_id, "重算后未生成新建议")
        payload["invalidated_reason"] = snapshot.basis_reason or "券后价依据发生变化"
        advice.payload = payload
        advice.superseded_by_id = supersede.get(advice.sku_id)
        advice.basis_status = "invalidated"
        advice.basis_reason = snapshot.basis_reason or "券后价依据发生变化"
        if advice.status == "pending":
            advice.status = "voided"
            advice.decide_note = f"依据失效自动作废：{advice.basis_reason}"
        changed.append(
            {
                "advice_id": advice.id,
                "run_no": run.run_no,
                "sku_id": advice.sku_id,
                "status": advice.status,
                "superseded_by_id": advice.superseded_by_id,
            }
        )
    db.flush()
    return changed

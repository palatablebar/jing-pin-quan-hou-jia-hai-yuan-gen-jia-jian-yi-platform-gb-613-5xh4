"""测算主流程：抓价回放 → 券台账核对（状态/名额/领取权）→ 算法 A（券后价还原）→ 算法 B（跟价建议）→ 建议入库。

口径：
  - effective   = 抓价当时的券后价（依据原样保留，便于复盘早班看到的价格）；
  - effective_visible = 核对券状态后「不靠已失效券」的可见价，算法 B 一律用它；
  - 同一抓价重跑走幂等领取（coupon_claims.claim_key），不重复领券、不重复耗名额；
  - 同日旧测算里依据已失效的建议：待审批的作废，并写入 superseded_by 指向本次新建议。

纯服务端计算，同样的券状态与规则两次运行结果一致；不访问外网。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.advice import Advice
from src.models.competitor import Competitor
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import coupon_service, effective_price, follow_advice, rule_service, run_service
from src.utils.timeutil import utcnow

CAPTURE_WINDOW_DAYS = 3


def _load_window(db: Session, captured_on: date) -> list[Snapshot]:
    start = captured_on - timedelta(days=CAPTURE_WINDOW_DAYS - 1)
    return list(
        db.scalars(
            select(Snapshot)
            .where(Snapshot.captured_on >= start, Snapshot.captured_on <= captured_on)
            .order_by(Snapshot.captured_on, Snapshot.id)
        ).all()
    )


def _refresh_coupons(db: Session, window: list[Snapshot], competitors: dict[int, Competitor], captured_on: date) -> dict:
    """逐条核对窗口内快照的券状态；仅对本次抓价日的快照执行幂等领取。"""
    summary = {
        "claims_new": 0,
        "claims_reused": 0,
        "claims_blocked": 0,
        "invalidated": 0,
        "unavailable_coupons": 0,
    }
    for snapshot in window:
        competitor = competitors.get(snapshot.competitor_id)
        coupon_service.link_coupons(db, snapshot, competitor=competitor)
        info = coupon_service.refresh_snapshot(
            db,
            snapshot,
            competitor=competitor,
            captured_on=captured_on,
            do_claim=True,
        )
        summary["claims_new"] += info["claims"]["claimed"]
        summary["claims_reused"] += info["claims"]["reused"]
        summary["claims_blocked"] += info["claims"]["blocked"]
        if info["invalidated"]:
            summary["invalidated"] += 1
            summary["unavailable_coupons"] += len(info["unavailable"])
    return summary


def _upsert_advice(db: Session, run: Run, existing: dict[int, Advice], sku: Sku, snapshot: Snapshot, result: dict) -> Advice:
    advice = existing.get(sku.id)
    effective = snapshot.effective or {}
    visible = snapshot.effective_visible or {}
    payload = {
        "algorithm": "B.follow_advice",
        "effective_algorithm": "A.effective_price",
        "current_price": round(sku.current_price, 2),
        "cost": round(sku.cost, 2),
        "floor_price": round(sku.floor_price, 2),
        "target_margin_pct": sku.target_margin_pct,
        "rule": result["strategy"],
        "cost_floor": result["cost_floor"],
        "market_cap": result["market_cap"],
        "clamped_by": result["clamped_by"],
        "breach": result["breach"],
        "reasons": result["reasons"],
        "p10": result["p10"],
        "p25": result["p25"],
        "p50": result["p50"],
        "mean": result["mean"],
        "samples": result["samples"],
        "basis_status": "valid",
        "basis_signature": coupon_service.basis_signature(snapshot),
        "basis_changed": snapshot.basis_status != "valid",
        "snapshot_basis_status": snapshot.basis_status,
        "basis_reason": snapshot.basis_reason,
        "priced_on": "effective_visible" if (visible.get("degraded") or snapshot.basis_status != "valid") else "effective",
        "page_price_visible": visible.get("page_price") or snapshot.page_price,
        "visible_unit_price_1": visible.get("unit_price_1"),
        "visible_degraded": bool(visible.get("degraded")),
        "unavailable_coupons": visible.get("excluded") or {},
        "snapshot": {
            "id": snapshot.id,
            "captured_on": snapshot.captured_on.isoformat(),
            "page_price": round(snapshot.page_price, 2),
            "full_reduction": snapshot.full_reduction or [],
            "shop_coupon": snapshot.shop_coupon,
            "platform_coupon": snapshot.platform_coupon,
            "member_discount": snapshot.member_discount,
            "unit_price_1": effective.get("unit_price_1"),
            "unit_price_2": effective.get("unit_price_2"),
            "unit_price_3": effective.get("unit_price_3"),
            "best_units": effective.get("best_units"),
            "note": effective.get("note"),
            "visible_unit_price_1": visible.get("unit_price_1"),
            "visible_unit_price_2": visible.get("unit_price_2"),
            "visible_unit_price_3": visible.get("unit_price_3"),
            "visible_note": visible.get("note"),
            "visible_degraded": bool(visible.get("degraded")),
            "basis_status": snapshot.basis_status,
            "basis_reason": snapshot.basis_reason,
        },
    }
    title = f"{sku.name}：竞品券后 ¥{result['p25']:.2f} → 建议 ¥{result['suggested_price']:.2f}"
    if snapshot.basis_status != "valid":
        title = f"{sku.name}：券后依据失效，按可见价 ¥{result['p25']:.2f} → 建议 ¥{result['suggested_price']:.2f}"
    summary = result["reason"]
    if snapshot.basis_reason:
        summary = f"{summary} 依据更新：{snapshot.basis_reason}。"
    if advice is None:
        advice = Advice(
            run_id=run.id,
            sku_id=sku.id,
            snapshot_id=snapshot.id,
            competitor_id=sku.competitor_id,
            rule_code=result["rule_code"],
            title=title[:200],
            summary=summary,
            action=result["action"],
            severity=result["risk"],
            score=abs(result["gap_pct"]),
            status="pending",
            current_price=sku.current_price,
            suggested_price=result["suggested_price"],
            effective_price=result["p25"],
            p10=result["p10"],
            p25=result["p25"],
            p50=result["p50"],
            margin_pct=result["margin_pct"],
            gap_pct=result["gap_pct"],
            payload=payload,
            basis_status="valid",
            basis_reason=snapshot.basis_reason,
        )
        db.add(advice)
    else:
        # 重算刷新数值，但绝不覆盖审批状态与审批意见
        advice.snapshot_id = snapshot.id
        advice.competitor_id = sku.competitor_id
        advice.rule_code = result["rule_code"]
        advice.title = title[:200]
        advice.summary = summary
        advice.action = result["action"]
        advice.severity = result["risk"]
        advice.score = abs(result["gap_pct"])
        advice.current_price = sku.current_price
        advice.suggested_price = result["suggested_price"]
        advice.effective_price = result["p25"]
        advice.p10 = result["p10"]
        advice.p25 = result["p25"]
        advice.p50 = result["p50"]
        advice.margin_pct = result["margin_pct"]
        advice.gap_pct = result["gap_pct"]
        advice.payload = payload
        advice.basis_status = "valid"
        advice.basis_reason = snapshot.basis_reason
    db.flush()
    return advice


def execute(db: Session, run: Run, captured_on: date, competitor_filter: int | None = None) -> dict:
    """执行一次测算，写入 run.metrics / run.log_text，返回 {metrics, log, status}。"""
    window = _load_window(db, captured_on)
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    rules = rule_service.effective_rules(db)

    coupon_summary = _refresh_coupons(db, window, competitors, captured_on)

    prices_by_sku: dict[int, list[float]] = {}
    invalid = 0
    degraded_samples = 0
    for snapshot in window:
        result = snapshot.effective or {}
        if not result or result.get("valid") is None:
            result = effective_price.compute_effective_price(
                {
                    "page_price": snapshot.page_price,
                    "full_reduction": snapshot.full_reduction or [],
                    "shop_coupon": snapshot.shop_coupon,
                    "platform_coupon": snapshot.platform_coupon,
                    "member_discount": snapshot.member_discount,
                }
            )
            snapshot.effective = result
        if not result["valid"]:
            invalid += 1
            continue
        visible_price = coupon_service.visible_unit_price(snapshot)
        if visible_price is None:
            continue
        if (snapshot.effective_visible or {}).get("degraded"):
            degraded_samples += 1
        prices_by_sku.setdefault(snapshot.sku_id, []).append(visible_price)

    targets = [
        snapshot
        for snapshot in window
        if snapshot.captured_on == captured_on
        and skus.get(snapshot.sku_id) is not None
        and skus[snapshot.sku_id].enabled
        and (competitor_filter is None or snapshot.competitor_id == competitor_filter)
    ]

    existing = {
        row.sku_id: row
        for row in db.scalars(select(Advice).where(Advice.run_id == run.id)).all()
    }
    advice_count = 0
    risk_bucket = {"high": 0, "medium": 0, "low": 0}
    gaps: list[float] = []
    discounts: list[float] = []
    priced_on_visible = 0
    supersede: dict[int, int | None] = {}
    resolution_by_sku: dict[int, str] = {}
    for snapshot in targets:
        sku = skus[snapshot.sku_id]
        competitor = competitors.get(snapshot.competitor_id)
        rule = rule_service.match_rule(rules, sku, competitor.code if competitor else None)
        prices = prices_by_sku.get(sku.id, [])
        if not prices:
            resolution_by_sku[sku.id] = "无可用券后价，未重算"
            continue
        try:
            result = follow_advice.build_advice(
                competitor_prices=prices,
                cost=sku.cost,
                floor_price=sku.floor_price,
                target_margin_pct=rule.target_margin_pct,
                current_price=sku.current_price,
                strategy=rule.strategy,
                max_drop_pct=rule.max_drop_pct,
            )
        except ValueError:
            invalid += 1
            resolution_by_sku[sku.id] = "竞品价缺失，未重算"
            continue
        result["rule_code"] = rule.code
        if (snapshot.effective or {}).get("discount_pct"):
            discounts.append(float(snapshot.effective["discount_pct"]))
        if (snapshot.effective_visible or {}).get("degraded") or snapshot.basis_status != "valid":
            priced_on_visible += 1
        if not result["need_follow"]:
            resolution_by_sku[sku.id] = "重算后不再建议跟价（可见价已回到安全区间）"
            continue
        advice = _upsert_advice(db, run, existing, sku, snapshot, result)
        supersede[sku.id] = advice.id
        resolution_by_sku[sku.id] = "已按可见价重算并生成新建议"
        advice_count += 1
        risk_bucket[result["risk"]] = risk_bucket.get(result["risk"], 0) + 1
        gaps.append(result["gap_pct"])

    db.flush()
    voided = coupon_service.void_stale_advices(
        db,
        captured_on=captured_on,
        run_id=run.id,
        supersede=supersede,
        resolution_by_sku=resolution_by_sku,
    )

    captured_at = utcnow()
    for competitor_id in {snapshot.competitor_id for snapshot in targets}:
        competitor = competitors.get(competitor_id)
        if competitor is not None:
            competitor.last_captured_at = captured_at

    metrics = {
        "window_snapshots": len(window),
        "snapshots": len(targets),
        "skus": len({snapshot.sku_id for snapshot in targets}),
        "advices": advice_count,
        "high": risk_bucket.get("high", 0),
        "medium": risk_bucket.get("medium", 0),
        "low": risk_bucket.get("low", 0),
        "avg_gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        "avg_discount_pct": round(sum(discounts) / len(discounts), 2) if discounts else 0.0,
        "invalid_snapshots": invalid,
        "rules_applied": len({rule.code for rule in rules}),
        "window_days": CAPTURE_WINDOW_DAYS,
        "captured_on": captured_on.isoformat(),
        "claims_new": coupon_summary["claims_new"],
        "claims_reused": coupon_summary["claims_reused"],
        "claims_blocked": coupon_summary["claims_blocked"],
        "invalidated_snapshots": coupon_summary["invalidated"],
        "degraded_samples": degraded_samples,
        "priced_on_visible": priced_on_visible,
        "voided_advices": len(voided),
    }
    log = (
        f"[{captured_on.isoformat()}] 回放窗口 {CAPTURE_WINDOW_DAYS} 天，快照 {len(window)} 条；"
        f"当日待测算 {len(targets)} 条 SKU；券后价还原失败 {invalid} 条；"
        f"领券：新领 {coupon_summary['claims_new']} / 复用早班 {coupon_summary['claims_reused']} / 被拦 {coupon_summary['claims_blocked']}；"
        f"券状态核对：依据失效快照 {coupon_summary['invalidated']} 条（按可见价重算 {priced_on_visible} 条 SKU）；"
        f"生成跟价建议 {advice_count} 条（高 {risk_bucket.get('high', 0)} / 中 {risk_bucket.get('medium', 0)} / 低 {risk_bucket.get('low', 0)}）；"
        f"作废旧依据建议 {len(voided)} 条；平均价差 {metrics['avg_gap_pct']}%，平均券后折扣 {metrics['avg_discount_pct']}%。"
    )
    status = "success" if metrics["snapshots"] > 0 else "partial"
    if invalid and invalid >= max(1, metrics["snapshots"] // 2):
        status = "partial"
    return {"metrics": metrics, "log": log, "status": status, "voided": voided}


def compute_run(
    db: Session,
    *,
    captured_on: date,
    operator_email: str,
    trigger: str = "manual",
    seed_key: str | None = None,
    competitor_id: int | None = None,
) -> Run:
    if seed_key:
        existing = db.scalar(select(Run).where(Run.seed_key == seed_key))
        if existing is not None:
            return existing
    run = Run(
        run_no=run_service.next_run_no(db),
        trigger=trigger,
        status="running",
        operator_email=operator_email,
        captured_on=captured_on,
        competitor_id=competitor_id,
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()
    try:
        result = execute(db, run, captured_on, competitor_id)
        run.metrics = result["metrics"]
        run.log_text = result["log"]
        run.status = result["status"]
        run.input_count = result["metrics"]["snapshots"]
        run.output_count = result["metrics"]["advices"]
    except Exception as exc:  # noqa: BLE001 - 失败留痕，不抛 500
        run.status = "failed"
        run.log_text = f"测算失败：{exc}"
        run.metrics = {"error": str(exc)[:300]}
    run.finished_at = utcnow()
    db.flush()
    return run

"""算法 A｜券可用性剔除：券不可领时，可见价必须不靠该券重算，并保留抓价口径。"""
from __future__ import annotations

from src.services.effective_price import compute_effective_price

SHOWPIECE = {
    "page_price": 299.0,
    "full_reduction": [{"threshold": 300.0, "minus": 40.0}],
    "shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True},
    "platform_coupon": {"per": 300.0, "minus": 50.0},
    "member_discount": 1.0,
}


def test_no_unavailable_matches_original_calculation():
    """不传 unavailable 时口径与历史版本逐字一致（验收样例不能漂移）。"""
    result = compute_effective_price(SHOWPIECE)
    assert (result["unit_price_1"], result["unit_price_2"], result["unit_price_3"]) == (279.0, 244.0, 245.67)
    assert result["best_units"] == 2
    assert result["degraded"] is False
    assert result["excluded"] == {}


def test_shop_coupon_gone_recomputes_without_it():
    """店铺券已抢空：1 件价回到页面价 299，不再按券面价 279 出建议。"""
    result = compute_effective_price(SHOWPIECE, unavailable={"shop_coupon": "券名额已抢空（已领 500/500）"})
    assert result["valid"] is True
    assert result["unit_price_1"] == 299.0
    # 2 件：598 − 40（满减）− 50（平台券） = 508 → 254
    assert result["unit_price_2"] == 254.0
    assert result["degraded"] is True
    assert result["excluded"] == {"shop_coupon": "券名额已抢空（已领 500/500）"}
    assert any("不可领" in warning for warning in result["warnings"])


def test_platform_coupon_gone_keeps_shop_coupon():
    result = compute_effective_price(SHOWPIECE, unavailable={"platform_coupon": "券已下线，当前不可领取"})
    # 1 件：299 − 20 = 279；2 件：598 − 40 − 20 = 538 → 269
    assert result["unit_price_1"] == 279.0
    assert result["unit_price_2"] == 269.0
    assert result["excluded"] == {"platform_coupon": "券已下线，当前不可领取"}


def test_all_coupons_gone_falls_back_to_page_price():
    result = compute_effective_price(
        SHOWPIECE,
        unavailable={"shop_coupon": "券已过期", "platform_coupon": "券已过期", "full_reduction": "活动已结束"},
    )
    assert result["unit_price_1"] == 299.0
    assert result["unit_price_2"] == 299.0
    assert result["unit_price_3"] == 299.0
    assert result["best_units"] == 1
    assert set(result["excluded"]) == {"shop_coupon", "platform_coupon", "full_reduction"}


def test_unavailable_input_is_not_mutated():
    spec = {
        "page_price": 299.0,
        "shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True},
    }
    snapshot_copy = {"page_price": 299.0, "shop_coupon": {"threshold": 200.0, "minus": 20.0, "stackable": True}}
    compute_effective_price(spec, unavailable={"shop_coupon": "券名额已抢空"})
    assert spec == snapshot_copy

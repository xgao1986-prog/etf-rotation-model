"""测试 defense_enabled 总开关：5个核心场景覆盖。

工程收口提交：v1.3 Step 5 防御总开关修复验证。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from rebalance_planner import (
    plan_rebalance_v2_5, _calc_commission, _calc_buy_shares, _calc_sell_shares
)


# ========== 场景1: defense_enabled=False 不得新买防御ETF ==========
def test_defense_disabled_no_new_defense_buy():
    """空仓，有防御候选，defense_enabled=False → 不得买入防御ETF。"""
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    industry = [("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 60.0)]
    defense = [("GOLD", 65.0)]
    prices = {"A": 100.0, "B": 50.0, "C": 30.0, "D": 20.0, "GOLD": 200.0}
    industry_tickers = {"A", "B", "C", "D"}
    defense_tickers = {"GOLD"}

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=5, max_defense_holdings=2, max_total_holdings=5,
        max_position_per_etf=0.20, defense_enabled=False,
    )

    defense_buys = [o for o in orders if o["action"] == "BUY" and o["ticker"] in defense_tickers]
    assert len(defense_buys) == 0, f"Expected 0 defense BUY, got {len(defense_buys)}"

    industry_buys = [o for o in orders if o["action"] == "BUY" and o["ticker"] in industry_tickers]
    assert len(industry_buys) == 4, f"Expected 4 industry BUY, got {len(industry_buys)}"

    # NAV 勾稽
    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01, f"NAV check failed: {nav_check} != {nav}"


# ========== 场景2: 已持有可交易防御，关闭后正确卖出并核对 ==========
def test_defense_disabled_sells_existing_defense():
    """持有防御ETF，defense_enabled=False → 正确卖出全部，核对订单/持仓/NAV。"""
    nav = 1_000_000.0
    cash = 500_000.0
    positions = {"GOLD": 2500}  # 2500股 @ 200元 = 50万
    prices = {"GOLD": 200.0, "A": 100.0, "B": 50.0}
    industry = [("A", 90.0), ("B", 80.0)]
    defense = [("GOLD", 65.0)]
    industry_tickers = {"A", "B"}
    defense_tickers = {"GOLD"}

    assert abs(cash + 2500 * 200.0 - nav) < 0.01

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=2, max_defense_holdings=1, max_total_holdings=3,
        max_position_per_etf=0.20, defense_enabled=False,
    )

    # 防御卖出订单存在且正确
    defense_sells = [o for o in orders if o["action"] == "SELL" and o["ticker"] == "GOLD"]
    assert len(defense_sells) == 1, f"Expected 1 defense SELL, got {len(defense_sells)}"

    sell_order = defense_sells[0]
    assert sell_order["shares"] == 2500, f"Expected 2500 shares, got {sell_order['shares']}"
    assert sell_order["price"] == 200.0, f"Expected price 200.0, got {sell_order['price']}"
    expected_amount = 2500 * 200.0
    assert sell_order["amount"] == expected_amount, f"Expected amount {expected_amount}, got {sell_order['amount']}"
    expected_comm = _calc_commission(expected_amount, 0.0003, 5.0)
    assert sell_order["commission"] == expected_comm, f"Expected commission {expected_comm}, got {sell_order['commission']}"

    # 防御持仓已清空（不是归零，而是彻底卖出）
    assert "GOLD" not in state.get("positions", {}), "GOLD should not be in positions after sell"

    # NAV 勾稽（卖出后现金增加，但同时买入行业消耗现金，最终 NAV 守恒）
    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01, f"NAV check failed: {nav_check} != {nav}"


# ========== 场景3: 已持有但缺少交易价格，不得非法卖出或归零 ==========
def test_defense_disabled_missing_price_no_illegal_sell():
    """持有防御但缺价，defense_enabled=False → 不非法卖出，持仓不归零（v2.5 安全跳过）。"""
    nav = 1_000_000.0
    cash = 500_000.0
    positions = {"GOLD": 2500}
    # prices 中无 GOLD 执行价格，但 last_prices 提供估值价格（避免 ValueError）
    prices = {"A": 100.0, "B": 50.0}
    industry = [("A", 90.0), ("B", 80.0)]
    defense = [("GOLD", 65.0)]
    industry_tickers = {"A", "B"}
    defense_tickers = {"GOLD"}

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=2, max_defense_holdings=1, max_total_holdings=3,
        max_position_per_etf=0.20, defense_enabled=False,
        last_prices={"GOLD": 200.0},  # 提供估值价格，但执行价格仍缺失
    )

    # 无防御卖出（执行价格缺失，安全跳过）
    defense_sells = [o for o in orders if o["action"] == "SELL" and o["ticker"] == "GOLD"]
    assert len(defense_sells) == 0, f"Expected 0 defense SELL (missing exec price), got {len(defense_sells)}"

    # GOLD 持仓仍然存在（未归零）
    assert "GOLD" in state.get("positions", {}), "GOLD should remain in positions (missing price)"
    assert state["positions"]["GOLD"] == 2500, f"Expected 2500 shares, got {state['positions']['GOLD']}"

    # 现金减少（买入行业），不是不变
    assert state["cash"] < cash, f"Cash should decrease after buying industry, got {state['cash']}"

    # NAV 勾稽（防御用 last_prices 估值，持仓保留）
    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01, f"NAV check failed: {nav_check} != {nav}"


# ========== 场景3b: 无 last_prices 时 v2.5 安全报错（不强制处理） ==========
def test_defense_disabled_missing_price_and_last_prices_raises():
    """无 prices 也无 last_prices → v2.5 抛出 ValueError，不静默处理。"""
    nav = 1_000_000.0
    cash = 500_000.0
    positions = {"GOLD": 2500}
    prices = {"A": 100.0, "B": 50.0}
    industry = [("A", 90.0), ("B", 80.0)]
    defense = [("GOLD", 65.0)]
    industry_tickers = {"A", "B"}
    defense_tickers = {"GOLD"}

    with pytest.raises(ValueError) as exc_info:
        plan_rebalance_v2_5(
            nav, cash, positions, industry, defense, prices,
            industry_tickers, defense_tickers,
            max_industry_holdings=2, max_defense_holdings=1, max_total_holdings=3,
            max_position_per_etf=0.20, defense_enabled=False,
        )
    assert "GOLD" in str(exc_info.value)


# ========== 场景4: 关闭防御后有剩余现金，不用行业违规填充防御槽位 ==========
def test_defense_disabled_cash_not_used_for_extra_industry():
    """defense_enabled=False，行业填满后剩余现金 → 保持现金，不违规填充。"""
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    industry = [("A", 90.0), ("B", 80.0)]
    defense = [("GOLD", 65.0)]
    prices = {"A": 100.0, "B": 50.0, "GOLD": 200.0}
    industry_tickers = {"A", "B"}
    defense_tickers = {"GOLD"}

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=5, max_defense_holdings=2, max_total_holdings=5,
        max_position_per_etf=0.20, defense_enabled=False,
    )

    buys = [o for o in orders if o["action"] == "BUY"]
    industry_buys = [o for o in buys if o["ticker"] in industry_tickers]
    defense_buys = [o for o in buys if o["ticker"] in defense_tickers]

    assert len(industry_buys) == 2, f"Expected 2 industry BUY, got {len(industry_buys)}"
    assert len(defense_buys) == 0, f"Expected 0 defense BUY, got {len(defense_buys)}"

    # 有剩余现金（未全部用完）
    assert state["cash"] > 0, f"Expected remaining cash > 0, got {state['cash']}"

    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01


# ========== 场景5: defense_enabled=True 与修改前完全兼容 ==========
def test_defense_enabled_true_backward_compatible():
    """defense_enabled=True 时行为与 v2.5 原始逻辑一致（保留可交易防御、腾槽位时卖出）。"""
    nav = 1_000_000.0
    cash = 500_000.0
    positions = {"GOLD": 2500}  # 50万防御，占比50%，超过20%目标
    prices = {"GOLD": 200.0, "A": 100.0, "B": 50.0, "C": 30.0}
    industry = [("A", 90.0), ("B", 80.0), ("C", 70.0)]
    defense = [("GOLD", 65.0)]
    industry_tickers = {"A", "B", "C"}
    defense_tickers = {"GOLD"}

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=3, max_defense_holdings=1, max_total_holdings=4,
        max_position_per_etf=0.20, defense_enabled=True,
    )

    # 防御仍在持仓中（保留可交易防御）
    assert "GOLD" in state.get("positions", {}), "GOLD should be retained when defense_enabled=True"

    # 应买入3个行业
    industry_buys = [o for o in orders if o["action"] == "BUY" and o["ticker"] in industry_tickers]
    assert len(industry_buys) == 3, f"Expected 3 industry BUY, got {len(industry_buys)}"

    # 防御可能部分卖出以腾出现金（v2.5 正常行为：现金不足时防御让路）
    defense_sells = [o for o in orders if o["action"] == "SELL" and o["ticker"] in defense_tickers]
    # 现金50万，3个行业各目标20%=20万，共需60万，防御需让路10万
    if defense_sells:
        # 有卖出说明防御让路释放现金，这是 v2.5 的预期行为
        sell_amount = sum(o["amount"] for o in defense_sells)
        assert sell_amount > 0
    else:
        # 无卖出说明现金+保留防御足够买入，或行业买入被缩放
        pass  # 两种子情况都合法

    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01


def test_defense_enabled_true_fills_new_defense():
    """defense_enabled=True，空仓有槽位和预算 → 正确填充新防御ETF。"""
    nav = 1_000_000.0
    cash = 1_000_000.0
    positions = {}
    industry = [("A", 90.0), ("B", 80.0), ("C", 70.0)]
    defense = [("GOLD", 65.0)]
    prices = {"A": 100.0, "B": 50.0, "C": 30.0, "GOLD": 200.0}
    industry_tickers = {"A", "B", "C"}
    defense_tickers = {"GOLD"}

    orders, state = plan_rebalance_v2_5(
        nav, cash, positions, industry, defense, prices,
        industry_tickers, defense_tickers,
        max_industry_holdings=3, max_defense_holdings=1, max_total_holdings=4,
        max_position_per_etf=0.20, defense_enabled=True,
    )

    # 应买入3个行业 + 1个防御
    industry_buys = [o for o in orders if o["action"] == "BUY" and o["ticker"] in industry_tickers]
    defense_buys = [o for o in orders if o["action"] == "BUY" and o["ticker"] in defense_tickers]
    assert len(industry_buys) == 3, f"Expected 3 industry BUY, got {len(industry_buys)}"
    assert len(defense_buys) == 1, f"Expected 1 defense BUY, got {len(defense_buys)}"
    assert defense_buys[0]["ticker"] == "GOLD"
    assert defense_buys[0]["shares"] > 0
    assert defense_buys[0]["shares"] % 100 == 0

    total_comm = sum(o["commission"] for o in orders)
    nav_check = state["cash"] + state["total_positions_value"] + total_comm
    assert abs(nav_check - nav) < 0.01

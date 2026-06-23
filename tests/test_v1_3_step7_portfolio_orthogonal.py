"""v1.3 Step 7: 组合集中度与资金去向正交拆解测试。

覆盖：
1. 四个方案配置正确性
2. 预注册验收标准评估逻辑
3. LOO读取CSV调用生产函数
4. B/C/D勾稽读取CSV验证
5. A精确复现
"""
import sys, os, pandas as pd, numpy as np
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from config import build_config, MARKET_REGIME_CONFIG, BENCHMARK, DEFENSE_UNIVERSE
from database import ETFDatabase
from market_regime import MarketRegimeDetector


def test_get_config_A():
    """A: 5×20% B0.4对照"""
    from v1_3_step7_portfolio_orthogonal_ab import get_config
    cfg = get_config("A")
    assert cfg["stock_max_holdings"] == 5
    assert cfg["max_holdings"] == 5
    assert cfg["total_max_holdings"] == 5
    assert cfg["defense_max_holdings"] == 2
    assert cfg["max_position_per_etf"] == 0.20
    assert cfg["fallback_equity_enabled"] is False


def test_get_config_B():
    """B: 4×20% + 现金，关闭防御"""
    from v1_3_step7_portfolio_orthogonal_ab import get_config
    cfg = get_config("B")
    assert cfg["stock_max_holdings"] == 4
    assert cfg["max_holdings"] == 4
    assert cfg["total_max_holdings"] == 4
    assert cfg["defense_max_holdings"] == 0
    assert cfg["max_position_per_etf"] == 0.20


def test_get_config_C():
    """C: 4×20% + 防御"""
    from v1_3_step7_portfolio_orthogonal_ab import get_config
    cfg = get_config("C")
    assert cfg["stock_max_holdings"] == 4
    assert cfg["max_holdings"] == 4
    assert cfg["total_max_holdings"] == 5
    assert cfg["defense_max_holdings"] == 1
    assert cfg["max_position_per_etf"] == 0.20


def test_get_config_D():
    """D: 4×25%"""
    from v1_3_step7_portfolio_orthogonal_ab import get_config
    cfg = get_config("D")
    assert cfg["stock_max_holdings"] == 4
    assert cfg["max_holdings"] == 4
    assert cfg["total_max_holdings"] == 4
    assert cfg["defense_max_holdings"] == 0
    assert cfg["max_position_per_etf"] == 0.25


def test_preregistration_loyo_majority():
    """预注册标准5：LOO严格多数D>A（>50%）。

    读取nav_A/B/C/D.csv，调用生产函数leave_one_year_out，
    验证只包含2019-2024（6年），证据不存在FAIL不skip。
    """
    from v1_3_step7_portfolio_orthogonal_ab import leave_one_year_out

    base = os.path.join(os.path.dirname(__file__), "..")
    paths = {
        "A": os.path.join(base, "reports", "v1_3_step7_nav_A.csv"),
        "B": os.path.join(base, "reports", "v1_3_step7_nav_B.csv"),
        "C": os.path.join(base, "reports", "v1_3_step7_nav_C.csv"),
        "D": os.path.join(base, "reports", "v1_3_step7_nav_D.csv"),
    }
    loyo_path = os.path.join(base, "reports", "v1_3_step7_loyo.csv")

    for sc, p in paths.items():
        assert os.path.exists(p), f"{sc}: NAV CSV 不存在: {p}"
    assert os.path.exists(loyo_path), f"LOO CSV 不存在: {loyo_path}"

    nav_a = pd.read_csv(paths["A"])
    nav_b = pd.read_csv(paths["B"])
    nav_c = pd.read_csv(paths["C"])
    nav_d = pd.read_csv(paths["D"])
    loyo_csv = pd.read_csv(loyo_path)

    loyo = leave_one_year_out(nav_a, nav_b, nav_c, nav_d)
    loyo_df = pd.DataFrame(loyo)

    # 1. 只含2019-2024
    years = [r["exclude_year"] for r in loyo]
    assert set(years) == {2019, 2020, 2021, 2022, 2023, 2024}
    assert len(loyo) == 6

    # 2. 与loyo.csv一致
    assert len(loyo_df) == len(loyo_csv)
    for idx in range(len(loyo_df)):
        assert abs(loyo_df.iloc[idx]["diff_da"] - loyo_csv.iloc[idx]["diff_da"]) < 0.0001

    # 3. 严格多数评估
    da_directions = [r["diff_da"] > 0 for r in loyo]
    majority = sum(da_directions) / len(da_directions)
    loyo_ok = majority > 0.5

    # 实际结果：D>A 5/6 = 83.3%，标准5判定通过
    assert loyo_ok == True, f"D>A: {sum(da_directions)}/{len(da_directions)} = {majority:.1%}"

    # 与CSV一致验证
    csv_directions = [r > 0 for r in loyo_csv["diff_da"]]
    csv_majority = sum(csv_directions) / len(csv_directions)
    assert abs(majority - csv_majority) < 0.01, f"LOO majority不一致: 函数={majority:.1%}, CSV={csv_majority:.1%}"


def test_bc_reconciliation_from_csv():
    """B/C/D勾稽测试读取CSV验证，不得硬编码。"""
    base = os.path.join(os.path.dirname(__file__), "..")
    scenarios = ["A", "B", "C", "D"]

    for sc in scenarios:
        nav_path = os.path.join(base, "reports", f"v1_3_step7_nav_{sc}.csv")
        trades_path = os.path.join(base, "reports", f"v1_3_step7_trades_{sc}.csv")
        recon_path = os.path.join(base, "reports", "v1_3_step7_reconciliation.csv")

        assert os.path.exists(nav_path), f"{sc}: NAV CSV 不存在"
        assert os.path.exists(trades_path), f"{sc}: trades CSV 不存在"

        nav_df = pd.read_csv(nav_path)
        trades_df = pd.read_csv(trades_path)

        # cash + positions_value = NAV
        if "cash" in nav_df.columns and "positions_value" in nav_df.columns:
            nav_df["check_nav"] = nav_df["cash"] + nav_df["positions_value"]
            mismatch = (nav_df["nav"] - nav_df["check_nav"]).abs()
            assert mismatch.max() < 0.01, f"{sc}: NAV勾稽失败"

        # 佣金生产公式
        if "commission" in trades_df.columns and "price" in trades_df.columns and "shares" in trades_df.columns:
            for _, row in trades_df.iterrows():
                expected = max(5.0, row["price"] * row["shares"] * 0.0003)
                actual = row["commission"]
                assert abs(actual - expected) < 0.01, f"{sc}: 佣金错误"

        num_trades = len(trades_df)
        assert num_trades > 0
        final_nav = nav_df["nav"].iloc[-1]
        assert final_nav > 0

        if os.path.exists(recon_path):
            recon_df = pd.read_csv(recon_path)
            recon_row = recon_df[recon_df["scenario"] == sc]
            if not recon_row.empty:
                assert abs(recon_row["final_nav"].iloc[0] - final_nav) < 0.01
                assert recon_row["num_trades"].iloc[0] == num_trades

        # A基线复现
        if sc == "A":
            assert abs(final_nav - 2_761_288.07) < 0.01, f"A NAV={final_nav}"
            assert num_trades == 804, f"A trades={num_trades}"


def test_defense_contribution_csv_exists():
    """防御贡献CSV存在且含黄金/国债字段。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_defense_contribution.csv")
    assert os.path.exists(path), f"防御贡献CSV不存在: {path}"
    df = pd.read_csv(path)
    assert "gold_pnl_b" in df.columns
    assert "gold_pnl_c" in df.columns
    assert "bond_pnl_b" in df.columns
    assert "bond_pnl_c" in df.columns


def test_reconciliation_csv_exists():
    """勾稽汇总CSV存在且含4个方案。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_reconciliation.csv")
    assert os.path.exists(path), f"reconciliation CSV不存在: {path}"
    df = pd.read_csv(path)
    assert set(df["scenario"].unique()) == {"A", "B", "C", "D"}

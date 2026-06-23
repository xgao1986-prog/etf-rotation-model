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


def test_position_exposure_csv_exists():
    """逐日敞口CSV存在且列名正确。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_position_exposure.csv")
    assert os.path.exists(path), f"position_exposure CSV不存在: {path}"
    df = pd.read_csv(path)
    for col in ["date", "scenario", "industry_pct", "defense_pct", "cash_pct", "top1_weight", "top4_weight"]:
        assert col in df.columns, f"position_exposure 缺少列: {col}"


def test_position_exposure_sum_to_one():
    """敞口 industry+defense+cash ≈ 100%。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_position_exposure.csv")
    assert os.path.exists(path)
    df = pd.read_csv(path)
    df["total"] = df["industry_pct"] + df["defense_pct"] + df["cash_pct"]
    max_dev = (df["total"] - 1.0).abs().max()
    assert max_dev < 0.001, f"敞口偏离100%: max_dev={max_dev:.4%}"


def test_slot_contribution_csv_exists():
    """槽位贡献CSV存在且含rank 1-5。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_slot_contribution.csv")
    assert os.path.exists(path), f"slot_contribution CSV不存在: {path}"
    df = pd.read_csv(path)
    for sc in ["A", "B", "C", "D"]:
        sub = df[df["scenario"] == sc]
        ranks = set(sub["rank"].unique()) if "rank" in sub.columns else set()
        assert ranks >= {1, 2, 3, 4}, f"{sc}: slot_contribution 缺少rank"
        # B/C/D 的 rank5 可以为0（因为无第5名）


def test_yearly_metrics_csv_exists():
    """年度指标CSV存在且含必要列。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_yearly_metrics.csv")
    assert os.path.exists(path), f"yearly_metrics CSV不存在: {path}"
    df = pd.read_csv(path)
    for col in ["year", "total_return", "sharpe", "max_drawdown", "n_trades", "total_commission"]:
        assert col in df.columns, f"yearly_metrics 缺少列: {col}"


def test_commission_summary_csv_exists():
    """佣金汇总CSV存在且含必要列。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_commission_summary.csv")
    assert os.path.exists(path), f"commission_summary CSV不存在: {path}"
    df = pd.read_csv(path)
    for col in ["year", "n_buys", "n_sells", "total_commission"]:
        assert col in df.columns, f"commission_summary 缺少列: {col}"


def test_standard7_verification_csv_exists():
    """预注册标准7验证CSV存在。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_standard7_verification.csv")
    assert os.path.exists(path), f"standard7_verification CSV不存在: {path}"
    df = pd.read_csv(path)
    assert "scenario" in df.columns
    assert "avg_top4_weight" in df.columns
    d_top4 = df[df["scenario"] == "D"]["avg_top4_weight"].iloc[0]
    a_top4 = df[df["scenario"] == "A"]["avg_top4_weight"].iloc[0]
    b_top4 = df[df["scenario"] == "B"]["avg_top4_weight"].iloc[0]
    assert d_top4 > a_top4, f"D Top4 ({d_top4}) 应 > A ({a_top4})"
    assert d_top4 > b_top4, f"D Top4 ({d_top4}) 应 > B ({b_top4})"


def test_orthogonal_attribution_csv_exists():
    """正交归因CSV存在且含所有对比对。"""
    path = os.path.join(os.path.dirname(__file__), "..", "reports", "v1_3_step7_orthogonal_attribution.csv")
    assert os.path.exists(path), f"orthogonal_attribution CSV不存在: {path}"
    df = pd.read_csv(path)
    pairs = set(df["pair"].unique()) if "pair" in df.columns else set()
    assert pairs >= {"B-A", "C-B", "D-B", "D-A"}, f"正交归因缺少对比对: {pairs}"

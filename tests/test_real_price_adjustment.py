#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_real_price_adjustment.py — 真实价格架构 v3（Codex 两轮复核修复）。

覆盖 docs/REAL_PRICE_ARCH_v3_DESIGN.md 第 4 节代表性验证：
1. 拆分后新建仓：成交价=市场价、股数/佣金/现金正确（真实价，非换算价）
2. 持仓跨拆分：股数×ratio、成本/ratio、总资产不腰斩不虚增
3. 成交量不造假：存储=真实量；评分量跨拆分连续，无虚假放量评分
4. 评分输入调整规则：adj_factor 应用于价、÷adj_factor 应用于量；无 adj_close 恒等
5. 拼接新语义：存真实 OHLCV + adj_close 连续，volume 不乘锚定因子
6. detect_splice_breaks 排除 corporate_actions 已登记拆分
"""

import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "paper_trading"))

from database import ETFDatabase, apply_adjustment_for_scoring
from data_fetcher import _splice_ticker_real_prices, detect_splice_breaks

BENCH = "000300.SH"
SPLIT_TICKER = "512480.SH"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _seed(db, ticker, rows):
    """rows: list of (date, open, high, low, close, volume, adj_close, source, adjust_type)"""
    df = pd.DataFrame(rows, columns=[
        "date", "open", "high", "low", "close",
        "volume", "adj_close", "source", "adjust_type",
    ])
    df["ticker"] = ticker
    # 绕过 INSERT OR IGNORE：直接覆盖写（测试用）
    with db._connect() as conn:
        for _, r in df.iterrows():
            conn.execute(
                "INSERT OR REPLACE INTO market_data "
                "(ticker,date,open,high,low,close,volume,adj_close,source,adjust_type) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ticker, r["date"], r["open"], r["high"], r["low"], r["close"],
                 r["volume"], r["adj_close"], r["source"], r["adjust_type"]),
            )
        conn.commit()


def _make_none_fwd(none_rows, fwd_rows):
    """构造 iFinD 双序列 DataFrame。rows: (date, open, high, low, close, volume)"""
    cols = ["ticker", "date", "open", "high", "low", "close", "volume"]
    none_df = pd.DataFrame(none_rows, columns=cols[1:])
    none_df["ticker"] = SPLIT_TICKER
    fwd_df = pd.DataFrame(fwd_rows, columns=cols[1:])
    fwd_df["ticker"] = SPLIT_TICKER
    return none_df[cols], fwd_df[cols]


# ----------------------------------------------------------------------
# 4. 评分输入调整规则
# ----------------------------------------------------------------------

class TestApplyAdjustmentForScoring:
    def test_no_adj_close_identity(self):
        """无 adj_close 列 → 原样返回（冻结路径恒等，B0.4 行为零变化）。"""
        df = pd.DataFrame({
            "ticker": ["A"], "date": ["2026-07-01"],
            "open": [2.0], "high": [2.2], "low": [1.9], "close": [2.1], "volume": [1e8],
        })
        out = apply_adjustment_for_scoring(df)
        assert out["close"].iloc[0] == 2.1
        assert out["volume"].iloc[0] == 1e8

    def test_adj_factor_one_identity(self):
        """adj_close == close（无拆分区间）→ 恒等，volume 不变。"""
        df = pd.DataFrame({
            "ticker": ["A"], "date": ["2026-07-01"],
            "open": [2.0], "high": [2.2], "low": [1.9], "close": [2.1], "volume": [1e8],
            "adj_close": [2.1],
        })
        out = apply_adjustment_for_scoring(df)
        assert out["close"].iloc[0] == pytest.approx(2.1)
        assert out["volume"].iloc[0] == pytest.approx(1e8)

    def test_split_boundary_price_halved_volume_doubled(self):
        """1:2 拆分（7/3 生效，post-split 锚定 adj）：评分价折半、评分量×2，各自连续。"""
        df = pd.DataFrame({
            "ticker": [SPLIT_TICKER] * 2,
            "date": ["2026-07-02", "2026-07-03"],
            "open": [5.40, 2.70], "high": [5.50, 2.80],
            "low": [5.30, 2.60], "close": [5.40, 2.70],
            "volume": [8e8, 16e8],
            # 拆分前 adj = close/2（折到拆后尺度）；拆分后 adj = close
            "adj_close": [2.70, 2.70],
        })
        out = apply_adjustment_for_scoring(df)
        # 真实价 7/2→7/3 是 -50% 假跳变；评分价应为 2.70→2.70（连续）
        assert out["close"].iloc[0] == pytest.approx(2.70, abs=1e-9)
        assert out["close"].iloc[1] == pytest.approx(2.70, abs=1e-9)
        # 真实量 7/3 是拆后口径（16 亿 = 2×8 亿）；评分量统一为拆后口径：1.6e9 → 1.6e9 连续
        assert out["volume"].iloc[0] == pytest.approx(1.6e9, rel=1e-9)
        assert out["volume"].iloc[1] == pytest.approx(1.6e9, rel=1e-9)

    def test_preserves_original_dataframe(self):
        """不应修改传入 df（调用方可能还要用真实价做交易）。"""
        df = pd.DataFrame({
            "ticker": ["A"], "date": ["2026-07-01"],
            "open": [2.0], "high": [2.2], "low": [1.9], "close": [2.1], "volume": [1e8],
            "adj_close": [2.1],
        })
        before = df.copy()
        apply_adjustment_for_scoring(df)
        pd.testing.assert_frame_equal(df, before)


# ----------------------------------------------------------------------
# 3/5. 拼接新语义：真实 OHLCV + 连续 adj_close + 真实成交量
# ----------------------------------------------------------------------

class TestSpliceRealPrices:
    """_splice_ticker_real_prices：存真实价（none）+ adj_close（fwd 校准）+ 真实量。"""

    def _seed_history(self, db):
        """库内 6/29 一行真实价（拆分前口径，adj=close）。"""
        _seed(db, SPLIT_TICKER, [(
            "2026-06-29", 2.90, 2.95, 2.85, 2.909, 9.65e8, 2.909,
            "iFinD-KimiGateway-correction", "none",
        )])

    def test_stores_real_prices_and_volumes(self, tmp_path):
        """7/2→7/3 1:2 拆分：存储 close=none 真实值；volume=iFinD 真实值（不乘因子）。"""
        db = ETFDatabase(str(tmp_path / "t.db"))
        self._seed_history(db)
        # none（真实）：6/29=2.909，7/2 收 5.40，7/3 拆分后 2.70；量 7/3 真实 16 亿
        # fwd（连续）：拆分日前 = none/2，拆分日起 = none
        none_df = pd.DataFrame({
            "ticker": SPLIT_TICKER,
            "date": ["2026-06-29", "2026-07-02", "2026-07-03"],
            "open": [2.90, 5.40, 2.70], "high": [2.95, 5.50, 2.80],
            "low": [2.85, 5.30, 2.60], "close": [2.909, 5.40, 2.70],
            "volume": [9.65e8, 8e8, 16e8],
        })
        fwd_df = pd.DataFrame({
            "ticker": SPLIT_TICKER,
            "date": ["2026-06-29", "2026-07-02", "2026-07-03"],
            "open": [1.45, 2.70, 2.70], "high": [1.475, 2.75, 2.80],
            "low": [1.425, 2.65, 2.60], "close": [1.4545, 2.70, 2.70],
            "volume": [9.65e8, 8e8, 16e8],
        })
        db_df = pd.DataFrame({
            "date": ["2026-06-29"], "close": [2.909],
        })
        result = {"gaps": [], "splice_factors": {}, "saved_count": 0,
                  "corporate_actions": []}
        info = {"gaps": [], "new_rows": 0, "latest_date": None, "error": None}
        cal = ["2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03"]

        _splice_ticker_real_prices(
            db, SPLIT_TICKER, db_df, none_df, fwd_df, cal, "2026-07-03",
            result, info,
        )

        got = db.get_market_data(ticker=SPLIT_TICKER)
        got["date"] = pd.to_datetime(got["date"]).dt.strftime("%Y-%m-%d")
        row_0702 = got[got["date"] == "2026-07-02"].iloc[0]
        row_0703 = got[got["date"] == "2026-07-03"].iloc[0]

        # 真实价存储：7/2=5.40（拆前），7/3=2.70（拆后）
        assert row_0702["close"] == pytest.approx(5.40, abs=1e-6)
        assert row_0703["close"] == pytest.approx(2.70, abs=1e-6)
        # 真实量存储：与 iFinD none 完全一致（7/2=8亿，7/3=16亿），无锚定因子放大
        assert row_0702["volume"] == pytest.approx(8e8, rel=1e-9)
        assert row_0703["volume"] == pytest.approx(16e8, rel=1e-9)
        # adj_close 连续：7/2 与 7/3 的 adj 收益率 ≈ 0（拆分无假跳变）
        adj_ret = row_0703["adj_close"] / row_0702["adj_close"] - 1
        assert abs(adj_ret) < 0.01
        # 拆分事件已登记（effective = 拆分后首个交易日 7/3，ratio=2）
        assert any(a["ticker"] == SPLIT_TICKER and a["effective_date"] == "2026-07-03"
                   and abs(a["ratio"] - 2.0) < 1e-9
                   for a in result["corporate_actions"])

    def test_no_anchor_factor_on_volume(self, tmp_path):
        """锚定因子 ≠1 时 volume 也不得改变：存储=none 原值。"""
        db = ETFDatabase(str(tmp_path / "t.db"))
        self._seed_history(db)
        # 场景：库内 6/29 close=2.909；6/30 无新拆分（none/fwd 比率保持 2.0 恒定）
        none_df = pd.DataFrame({
            "ticker": SPLIT_TICKER, "date": ["2026-06-29", "2026-06-30"],
            "open": [2.90, 2.95], "high": [2.95, 3.00],
            "low": [2.85, 2.90], "close": [2.909, 2.98],
            "volume": [9.65e8, 1.1e9],
        })
        fwd_df = pd.DataFrame({
            "ticker": SPLIT_TICKER, "date": ["2026-06-29", "2026-06-30"],
            "open": [1.45, 1.475], "high": [1.475, 1.50],
            "low": [1.425, 1.45], "close": [1.4545, 1.49],
            "volume": [9.65e8, 1.1e9],
        })
        db_df = pd.DataFrame({"date": ["2026-06-29"], "close": [2.909]})
        result = {"gaps": [], "splice_factors": {}, "saved_count": 0,
                  "corporate_actions": []}
        info = {"gaps": [], "new_rows": 0, "latest_date": None, "error": None}

        _splice_ticker_real_prices(
            db, SPLIT_TICKER, db_df, none_df, fwd_df,
            ["2026-06-30"], "2026-06-30", result, info,
        )
        got = db.get_market_data(ticker=SPLIT_TICKER)
        got["date"] = pd.to_datetime(got["date"]).dt.strftime("%Y-%m-%d")
        row = got[got["date"] == "2026-06-30"].iloc[0]
        # volume = none 原值，未乘校准因子 2.0
        assert row["volume"] == pytest.approx(1.1e9, rel=1e-9)
        # adj_close 与库内尺度衔接：adj(6/30) ≈ close(6/30)
        assert row["adj_close"] == pytest.approx(2.98, abs=1e-6)
        # 比率恒定 → 不登记拆分
        assert result["corporate_actions"] == []


class TestDetectSpliceBreaksExcludesKnownSplits:
    def test_known_split_not_flagged(self, tmp_path):
        """corporate_actions 登记的拆分日收益率 -50% 不得报为断裂。"""
        db = ETFDatabase(str(tmp_path / "t.db"))
        db.save_corporate_actions(pd.DataFrame([{
            "ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
            "action_type": "split", "ratio": 2.0, "source": "test", "note": "",
        }]))
        days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
        closes = [5.40, 5.38, 2.69, 2.72]  # 7/3 = -50%（真实拆分）
        _seed(db, SPLIT_TICKER, [(
            d, c, c, c, c, 1e8, c, "TEST", "none") for d, c in zip(days, closes)])

        breaks = detect_splice_breaks(db, [SPLIT_TICKER])
        assert breaks == {}

    def test_unknown_break_still_flagged(self, tmp_path):
        """未登记的 -49% 跳变仍须报为断裂（不能把真实断裂误判为拆分）。"""
        db = ETFDatabase(str(tmp_path / "t.db"))
        days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
        closes = [5.40, 5.38, 2.69, 2.72]
        _seed(db, SPLIT_TICKER, [(
            d, c, c, c, c, 1e8, c, "TEST", "none") for d, c in zip(days, closes)])

        breaks = detect_splice_breaks(db, [SPLIT_TICKER])
        assert SPLIT_TICKER in breaks
        assert breaks[SPLIT_TICKER][0]["date"] == "2026-07-03"


# ----------------------------------------------------------------------
# 1/2. 虚拟盘：真实价成交 + 跨拆分持仓调整
# ----------------------------------------------------------------------

class TestPaperTradingRealPrices:
    """生产入口 run_daily 验证：真实价成交 + 跨拆分持仓调整。"""

    def _make_service_runner(self, tmp_path):
        from paper_trading.store import PaperTradingStore
        from paper_trading.service import PaperTradingService
        from paper_trading.runner import PaperTradingRunner
        store = PaperTradingStore(str(tmp_path / "paper.db"))
        service = PaperTradingService(store)
        return service, PaperTradingRunner(service)

    def test_buy_after_split_uses_real_price(self, tmp_path):
        """拆分后新建仓：7/2 信号 65 分 → 7/3 按真实开盘价 2.70 成交（非拆前口径 5.40）。

        佣金=max(股数×价×0.0003, 5)；现金扣减与成交一致；股数整手。
        """
        from paper_trading.models import AccountCreate, AccountType, StartMode
        service, runner = self._make_service_runner(tmp_path)
        service.create_account(AccountCreate(
            account_id="ACC-BUY-1", name="拆分后买入", account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.CASH,
            start_date="2026-07-01",  # 须早于信号日：建户已把 start_date 记为已处理
        ))

        scores = pd.DataFrame({"ticker": [SPLIT_TICKER], "total_score": [65]})
        runner.run_daily("ACC-BUY-1", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40}, scores)
        result = runner.run_daily("ACC-BUY-1", "2026-07-03",
                                  {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70})

        trades = result["trades"]
        assert len(trades) == 1
        t = trades[0]
        # 成交价 = 真实市场价 2.70（等效口径会是 5.40）
        assert t["price"] == pytest.approx(2.70, abs=1e-9)
        assert t["action"] == "BUY"
        shares = t["shares"]
        assert shares % 100 == 0  # 整手
        assert shares > 0
        # 佣金规则
        expected_comm = max(shares * 2.70 * 0.0003, 5.0)
        assert t["commission"] == pytest.approx(expected_comm, abs=1e-6)
        # 现金：1,000,000 - 股数×2.70 - 佣金
        assert result["cash"] == pytest.approx(
            1_000_000 - shares * 2.70 - expected_comm, abs=1e-6)
        # 持仓市值按真实收盘估值
        pos = result["positions"][SPLIT_TICKER]
        assert pos["shares"] == shares
        assert pos["last_price"] == pytest.approx(2.70, abs=1e-9)

    def test_position_crossing_split_adjusted(self, tmp_path):
        """持仓跨拆分：7/2 持仓 1000 股@5.40 → 7/3 拆分 1:2 盘前调整
        2000 股@2.70；市值与总资产不变（不腰斩、不虚增）；不触发止损。"""
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-SPLIT-1", name="跨拆分", account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))

        r1 = runner.run_daily("ACC-SPLIT-1", "2026-07-02",
                              {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        nav_before = r1["nav"]
        assert r1["positions"][SPLIT_TICKER]["market_value"] == pytest.approx(5400.0, abs=1e-6)

        corp = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                 "action_type": "split", "ratio": 2.0, "status": "confirmed"}]
        r2 = runner.run_daily("ACC-SPLIT-1", "2026-07-03",
                              {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                              corporate_actions=corp)

        pos = r2["positions"][SPLIT_TICKER]
        assert pos["shares"] == 2000                 # 股数 ×2
        assert pos["cost_price"] == pytest.approx(2.70, abs=1e-9)   # 成本 ÷2
        assert pos["market_value"] == pytest.approx(5400.0, abs=1e-6)  # 市值不变
        assert r2["nav"] == pytest.approx(nav_before, abs=1e-6)        # 总资产不变
        assert r2["trades"] == []                  # 未误触发止损清仓

    def test_no_corp_actions_no_change(self, tmp_path):
        """无拆分日传入 corporate_actions=None 时行为与现状完全一致。"""
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        service.create_account(AccountCreate(
            account_id="ACC-NONE-1", name="无拆分", account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=999_000.0,
            opening_positions=(OpeningPosition("512400.SH", 1000, 1.0, 1.0),),
        ))
        r = runner.run_daily("ACC-NONE-1", "2026-07-02", {"512400.SH": 1.0}, {"512400.SH": 1.0})
        assert r["positions"]["512400.SH"]["shares"] == 1000


class TestCorporateActionConfirmationGate:
    """Trae ROUND3 确认闸门：仅 confirmed 可调整持仓；pending 禁止自动执行。

    覆盖 docs/TRAe_REVIEW_REQUEST_ROUND3 意见三/四：
    - 拆分当日卖出：盘先调整持仓，再按真实价卖出调整后股数
    - 止损不误触发：待确认拆分当日价差不触发止损
    - 重复调整防护：同标的同日多条 confirmed 只应用一次
    - pending/缺省 status 不自动执行、不加止损、留痕 CORPORATE_ACTION
    - 份额合并（ratio<1，516160 场景）：股数减少、成本上升、市值不变
    """

    def _make_service_runner(self, tmp_path):
        from paper_trading.store import PaperTradingStore
        from paper_trading.service import PaperTradingService
        from paper_trading.runner import PaperTradingRunner
        store = PaperTradingStore(str(tmp_path / "paper.db"))
        service = PaperTradingService(store)
        return service, PaperTradingRunner(service)

    CONFIRMED_SPLIT = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                        "action_type": "split", "ratio": 2.0, "status": "confirmed"}]

    def test_sell_on_split_day_uses_adjusted_shares(self, tmp_path):
        """拆分当日卖出：盘前股数 1000→2000，随后按真实开盘价 2.70 卖出 2000 股。

        成交总额=2000×2.70（=拆前市值 5400），现金按真实价回收，不腰斩。
        """
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-SELL-SPLIT", name="拆分日卖出",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-01",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        # 7/1 建状态；7/2（周四调仓日）低分（10 < min 40）→ 准备 7/3 全卖信号
        runner.run_daily("ACC-SELL-SPLIT", "2026-07-01",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        low = pd.DataFrame({"ticker": [SPLIT_TICKER], "total_score": [10]})
        runner.run_daily("ACC-SELL-SPLIT", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40}, low)
        # 7/3 拆分生效日：盘前调整 2000 股，执行昨日卖出信号，真实开盘价 2.70
        r = runner.run_daily("ACC-SELL-SPLIT", "2026-07-03",
                             {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                             corporate_actions=self.CONFIRMED_SPLIT)
        sells = [t for t in r["trades"] if t["action"] == "SELL"]
        assert len(sells) == 1
        t = sells[0]
        assert t["shares"] == 2000              # 卖出的是调整后股数
        assert t["price"] == pytest.approx(2.70, abs=1e-9)  # 真实价，非拆前 5.40
        # 回收现金 = 2000×2.70 - 佣金（与 994600 + 5400 - 佣金一致，市值不腰斩）
        expected_comm = max(2000 * 2.70 * 0.0003, 5.0)
        assert t["commission"] == pytest.approx(expected_comm, abs=1e-6)
        assert r["cash"] == pytest.approx(
            994_600.0 + 2000 * 2.70 - expected_comm, abs=1e-4)
        assert SPLIT_TICKER not in r["positions"]
        # NAV = 现金（持仓已清空），总资产 = 994600+5400-佣金+剩余现金零头
        assert r["nav"] == pytest.approx(r["cash"], abs=1e-6)

    def test_pending_action_pauses_account_run(self, tmp_path):
        """status=pending 且涉及持仓：账户运行整体暂停（ROUND5 安全处理新语义）。

        7/3 开盘价 2.70 相对成本 5.40 是 -50%，若照常运行会误触 -8% 止损并
        保存错误的拆分后状态。新语义要求：返回 paused 结果、不生成交易、
        不估值、不保存当日状态（当日保持未处理，确认后可重跑）。
        """
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-PENDING", name="待确认拆分",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        runner.run_daily("ACC-PENDING", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        pending = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                    "action_type": "split", "ratio": 2.0}]  # 无 status = pending
        r = runner.run_daily("ACC-PENDING", "2026-07-03",
                             {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                             corporate_actions=pending)
        # 整体暂停：明确提示待确认
        assert r["paused"] is True
        assert SPLIT_TICKER in r["reason"] and "待确认" in r["reason"]
        # 无交易、无订单、无信号
        assert r["trades"] == [] and r["orders"] == [] and r["signals"] == []
        # 持仓保持拆前口径（未被调整）
        pos = r["positions"][SPLIT_TICKER]
        assert pos["shares"] == 1000
        assert pos["cost_price"] == pytest.approx(5.40, abs=1e-9)
        # 当日未被标记为已处理：确认后可重跑同一日（幂等恢复）
        assert not service.store.is_day_processed("ACC-PENDING", "2026-07-03")
        # 数据库中不存在 7/3 的任何记录：不得保存错误账户状态
        assert service.store.get_nav("ACC-PENDING", "2026-07-03") is None
        # 确认后重跑同一日：正常调整并继续（2000 股 @ 2.70 成本）
        r2 = runner.run_daily("ACC-PENDING", "2026-07-03",
                              {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                              corporate_actions=self.CONFIRMED_SPLIT)
        assert not r2.get("paused")
        pos2 = r2["positions"][SPLIT_TICKER]
        assert pos2["shares"] == 2000
        assert pos2["cost_price"] == pytest.approx(2.70, abs=1e-9)
        # NAV 无虚假亏损：1000×5.40 = 2000×2.70
        assert r2["nav"] == pytest.approx(1_000_000.0, abs=0.02)

    def test_pending_cross_two_days_no_false_state(self, tmp_path):
        """跨两个交易日：pending 涉持仓两天都暂停，无虚假亏损/错误止损/错误资金分配。

        第 1 天（7/3）与第 2 天（7/6）均未确认：两天都必须整体暂停、不保存
        任何状态；第 2 天的基线回退到 7/2（最后已保存日），持仓与现金保持
        拆前口径，不得出现由 -50% 价差虚构的亏损、不得错误止损、不得把
        资金分配给任何新交易。
        """
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-XDAY", name="跨日待确认",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        runner.run_daily("ACC-XDAY", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        pending = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                    "action_type": "split", "ratio": 2.0}]  # 无 status = pending
        # 第 1 天：暂停
        r1 = runner.run_daily("ACC-XDAY", "2026-07-03",
                              {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                              corporate_actions=pending)
        assert r1["paused"] is True and r1["trades"] == []
        # 第 2 天（7/6，下一交易日）：仍未确认，继续暂停
        r2 = runner.run_daily("ACC-XDAY", "2026-07-06",
                              {SPLIT_TICKER: 2.72}, {SPLIT_TICKER: 2.72},
                              corporate_actions=pending)
        assert r2["paused"] is True
        assert r2["trades"] == [] and r2["orders"] == [] and r2["signals"] == []
        # 无虚假亏损：暂停结果的 NAV 用基线最后价计算，持仓仍是拆前口径
        pos = r2["positions"][SPLIT_TICKER]
        assert pos["shares"] == 1000 and pos["cost_price"] == pytest.approx(5.40, abs=1e-9)
        # 无错误止损：7/3 与 7/6 均无 STOP_LOSS 交易落库
        t3 = service.store.list_trades("ACC-XDAY", trade_date="2026-07-03")
        t6 = service.store.list_trades("ACC-XDAY", trade_date="2026-07-06")
        assert t3 == [] and t6 == []
        # 无错误资金分配：现金与 7/2 结束时一致；7/3、7/6 无 NAV 记录
        assert r2["cash"] == pytest.approx(994_600.0, abs=1e-6)
        assert service.store.get_nav("ACC-XDAY", "2026-07-03") is None
        assert service.store.get_nav("ACC-XDAY", "2026-07-06") is None
        # 未标记已处理：确认后可以从 7/3 起逐日重跑恢复
        assert not service.store.is_day_processed("ACC-XDAY", "2026-07-03")
        assert not service.store.is_day_processed("ACC-XDAY", "2026-07-06")
        # 确认后重跑第 1 天：调整生效、无亏损（NAV 连续）
        r1c = runner.run_daily("ACC-XDAY", "2026-07-03",
                               {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                               corporate_actions=self.CONFIRMED_SPLIT)
        assert not r1c.get("paused")
        assert r1c["nav"] == pytest.approx(1_000_000.0, abs=0.02)

    def test_duplicate_confirmed_applied_once(self, tmp_path):
        """同一标的同日传入两条 confirmed 记录：只应用一次（2000 股而非 4000）。"""
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-DUP", name="重复调整防护",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        runner.run_daily("ACC-DUP", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        dup = self.CONFIRMED_SPLIT + [dict(self.CONFIRMED_SPLIT[0])]
        r = runner.run_daily("ACC-DUP", "2026-07-03",
                             {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                             corporate_actions=dup)
        pos = r["positions"][SPLIT_TICKER]
        assert pos["shares"] == 2000                    # ×2 一次，不是 ×4
        assert pos["cost_price"] == pytest.approx(2.70, abs=1e-9)
        assert pos["market_value"] == pytest.approx(5400.0, abs=1e-4)

    def test_consolidation_ratio_below_one(self, tmp_path):
        """份额合并（516160 场景，ratio=0.30912005<1）：股数减、成本升、市值不变。"""
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition("516160.SH", 10000, 3.236, 3.236)]
        service.create_account(AccountCreate(
            account_id="ACC-MERGE", name="份额合并",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2024-09-11",
            opening_cash=967_640.0, opening_positions=tuple(holdings),
        ))
        # 9/11 建状态；9/12（周四）取合并前 NAV 基线；9/13 中秋前最后交易日
        runner.run_daily("ACC-MERGE", "2024-09-11",
                         {"516160.SH": 3.236}, {"516160.SH": 3.236})
        nav_before = runner.run_daily("ACC-MERGE", "2024-09-12",
                                      {"516160.SH": 3.24}, {"516160.SH": 3.24})["nav"]
        # 2024-09-18 除权：真实价 10.47（=3.236/0.30912005 附近），confirmed 合并
        merge = [{"ticker": "516160.SH", "effective_date": "2024-09-18",
                  "action_type": "split", "ratio": 0.30912005, "status": "confirmed"}]
        new_shares = 10000 * 0.30912005  # 持仓股数不取整，与拆分实现一致
        px = 3.236 / 0.30912005
        r = runner.run_daily("ACC-MERGE", "2024-09-18",
                             {"516160.SH": px}, {"516160.SH": px},
                             corporate_actions=merge)
        pos = r["positions"]["516160.SH"]
        assert pos["shares"] == pytest.approx(new_shares, rel=1e-9)
        assert pos["cost_price"] == pytest.approx(3.236 / 0.30912005, rel=1e-9)
        assert pos["market_value"] == pytest.approx(10000 * 3.236, rel=1e-3)
        assert r["nav"] == pytest.approx(nav_before, rel=1e-4)
        assert r["trades"] == []  # 合并不触发止损

    def test_pending_not_held_suspends_buy_until_confirmed(self, tmp_path):
        """pending 未持仓：暂停该标的新买入资格，跨交易日持续直到确认。

        用 _apply_corporate_actions 直接验证裁决语义：同一 pending 记录
        在第 1 天（生效日）和第 2 天（已过生效日仍未确认）都必须出现在
        suspended_buy 中；确认（status=confirmed）后同一日期不再禁买。
        """
        service, runner = self._make_service_runner(tmp_path)
        other = "512010.SH"  # 未持仓标的
        state = {"cash": 994_600.0, "positions": {
            SPLIT_TICKER: {"shares": 1000, "cost_price": 5.40, "last_price": 5.40},
        }}
        pending = [{"ticker": other, "effective_date": "2026-07-03",
                    "action_type": "split", "ratio": 2.0, "status": "pending"}]
        # 第 1 天（生效日）：未持仓 → 禁买，不暂停账户
        _, pause1, susp1 = runner._apply_corporate_actions(
            state, "2026-07-03", pending)
        assert pause1 is None
        assert other in susp1
        # 第 2 天（已过了生效日仍未确认）：跨交易日持续禁买
        _, pause2, susp2 = runner._apply_corporate_actions(
            state, "2026-07-06", pending)
        assert pause2 is None
        assert other in susp2, "pending 未确认时禁买必须跨交易日持续"
        # 确认后：同一日期不再禁买（生效日已过，confirmed 也不再调整）
        confirmed = [dict(pending[0], status="confirmed")]
        _, pause3, susp3 = runner._apply_corporate_actions(
            state, "2026-07-06", confirmed)
        assert pause3 is None and other not in susp3
        # 涉持仓的 pending：持仓标的触发暂停账户（与是否生效日无关，已生效即暂停）
        held_pending = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                         "action_type": "split", "ratio": 2.0, "status": "pending"}]
        _, pause4, _ = runner._apply_corporate_actions(
            state, "2026-07-03", held_pending)
        assert pause4 is not None and pause4["ticker"] == SPLIT_TICKER
        # 未来生效的 pending 涉持仓：暂不处理（未到生效日）
        future_pending = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-10",
                           "action_type": "split", "ratio": 2.0, "status": "pending"}]
        _, pause5, _ = runner._apply_corporate_actions(
            state, "2026-07-03", future_pending)
        assert pause5 is None


class TestRound6RecoveryGuards:
    """ROUND6 补漏：恢复与异常场景防护（用户认可暂停方向后的两个补充）。

    ① 生效日暂停 → 隔日确认 → 直接运行隔日：必须阻止并提示先补跑生效日，
       不能带着旧股数恢复运行。
    ② 公司行动记录查询失败：停止本次账户运行并明确报错，不能当作没有
       记录继续。
    """

    def _make_service_runner(self, tmp_path):
        from paper_trading.store import PaperTradingStore
        from paper_trading.service import PaperTradingService
        from paper_trading.runner import PaperTradingRunner
        store = PaperTradingStore(str(tmp_path / "pt.db"))
        service = PaperTradingService(store)
        return service, PaperTradingRunner(service)

    def test_confirmed_missed_effective_date_blocks_jump(self, tmp_path):
        """① 生效日暂停 → 隔日确认 → 直接运行隔日：阻止并提示先补跑生效日。

        7/3 pending 暂停（未保存）；7/6 记录已确认但 7/3 从未处理：直接续跑
        会带着 1000 旧股数面对 2.70 拆后价（-50%），立刻虚假亏损并错误止损。
        必须整体暂停、提示补跑 7/3、不落任何状态；补跑 7/3 后 7/6 正常恢复。
        """
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-JUMP", name="跳期防护",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        runner.run_daily("ACC-JUMP", "2026-07-02",
                         {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        # 7/3：pending 暂停，未保存
        pending = [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                    "action_type": "split", "ratio": 2.0, "status": "pending"}]
        r1 = runner.run_daily("ACC-JUMP", "2026-07-03",
                              {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                              corporate_actions=pending)
        assert r1["paused"] is True
        # 7/6：记录已确认但 7/3 未处理 → 跳期防护暂停
        r2 = runner.run_daily("ACC-JUMP", "2026-07-06",
                              {SPLIT_TICKER: 2.72}, {SPLIT_TICKER: 2.72},
                              corporate_actions=self._confirmed())
        assert r2["paused"] is True
        assert "2026-07-03" in r2["reason"] and "补跑" in r2["reason"]
        assert r2["trades"] == [] and r2["orders"] == [] and r2["signals"] == []
        # 不保存错误账户状态：7/6 无 NAV、无交易、未标记已处理
        assert service.store.get_nav("ACC-JUMP", "2026-07-06") is None
        assert service.store.list_trades("ACC-JUMP", trade_date="2026-07-06") == []
        assert not service.store.is_day_processed("ACC-JUMP", "2026-07-06")
        # 持仓仍是旧股数口径（未被静默调整）
        pos = r2["positions"][SPLIT_TICKER]
        assert pos["shares"] == 1000 and pos["cost_price"] == pytest.approx(5.40, abs=1e-9)
        # 先补跑 7/3：确认后重跑生效日 → 正常调整
        r3 = runner.run_daily("ACC-JUMP", "2026-07-03",
                              {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70},
                              corporate_actions=self._confirmed())
        assert not r3.get("paused")
        assert r3["positions"][SPLIT_TICKER]["shares"] == 2000
        assert r3["positions"][SPLIT_TICKER]["cost_price"] == pytest.approx(2.70, abs=1e-9)
        # 再跑 7/6：正常恢复，无虚假亏损（2.72 vs 成本 2.70 = +0.7%，不触发止损）
        r4 = runner.run_daily("ACC-JUMP", "2026-07-06",
                              {SPLIT_TICKER: 2.72}, {SPLIT_TICKER: 2.72},
                              corporate_actions=self._confirmed())
        assert not r4.get("paused")
        assert r4["trades"] == []
        assert r4["nav"] == pytest.approx(994_600.0 + 2000 * 2.72, abs=0.02)

    def test_corporate_actions_query_failure_stops_run(self, tmp_path):
        """② 公司行动记录查询失败：停止本次账户运行并明确报错。

        provider 抛异常时不得当作"没有记录"继续运行——否则未确认拆分会
        静默漏调整。必须返回 failure、不生成交易、不保存任何账户状态。
        """
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        from paper_trading.ui_service import PaperTradingUIService
        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-QFAIL", name="查询失败",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        # create_account 已把 start_date 记为已处理，无需额外标记

        def _boom(date_str):
            raise RuntimeError("disk I/O error")

        ui = PaperTradingUIService(service, runner, corporate_actions_provider=_boom)
        results = ui.run_accounts(
            ["ACC-QFAIL"], "2026-07-03",
            {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70}, pd.DataFrame(),
        )
        # 明确报错并停止
        assert results["success"] == {}
        assert "ACC-QFAIL" in results["failure"]
        assert "公司行动记录查询失败" in results["failure"]["ACC-QFAIL"]
        # 不生成交易、不保存任何状态
        assert service.store.list_trades("ACC-QFAIL", trade_date="2026-07-03") == []
        assert service.store.get_nav("ACC-QFAIL", "2026-07-03") is None
        assert not service.store.is_day_processed("ACC-QFAIL", "2026-07-03")

    @staticmethod
    def _confirmed():
        return [{"ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
                 "action_type": "split", "ratio": 2.0, "status": "confirmed"}]

    def test_page_query_entry_full_flow_backfill_required(self, tmp_path, monkeypatch):
        """页面查询入口完整流程：生效日暂停 → 隔日确认 → 直接运行隔日。

        通过页面真实查询入口 app._load_corporate_actions（注入临时 ETFDatabase，
        不绕过、不手工传记录）驱动 ui_service.run_accounts：
        1. 7/3 pending 暂停（skipped，无状态保存）
        2. 库中确认记录后，7/6 直接运行 → 必须提示先补跑 2026-07-03，
           不得带着 1000 旧股数面对 2.72 拆后价（否则 -49.6% 立刻错误止损）
        3. 补跑 7/3（页面查询同样驱动）→ 股数正常调整 2000 @ 2.70
        4. 再跑 7/6 → 正常继续，无暂停无止损，NAV 连续
        """
        import app as app_mod
        from paper_trading.models import (
            AccountCreate, AccountType, OpeningPosition, StartMode,
        )
        from paper_trading.ui_service import PaperTradingUIService

        # 临时行情库：仅登记 512480 2026-07-03 拆分（先 pending 后 confirm）
        ca_db = ETFDatabase(db_path=str(tmp_path / "etf_ca.db"))
        ca_db.save_corporate_actions(pd.DataFrame([{
            "ticker": SPLIT_TICKER, "effective_date": "2026-07-03",
            "action_type": "split", "ratio": 2.0,
            "note": "1:2 份额拆分",
        }]))
        # 页面入口用 ETFDatabase() 默认连接：替换为临时库（页面查询函数本身不改）
        monkeypatch.setattr(app_mod, "ETFDatabase",
                            lambda *a, **k: ca_db)
        provider = app_mod._load_corporate_actions
        # provider 必须能看到 pending 记录（未来生效日也返回，供跨日禁买/暂停判定）
        recs = provider("2026-07-02")
        assert recs and any(r["ticker"] == SPLIT_TICKER for r in recs)

        service, runner = self._make_service_runner(tmp_path)
        holdings = [OpeningPosition(SPLIT_TICKER, 1000, 5.40, 5.40)]
        service.create_account(AccountCreate(
            account_id="ACC-PAGE", name="页面入口流程",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4", strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000.0, start_mode=StartMode.IMPORTED,
            start_date="2026-07-02",
            opening_cash=994_600.0, opening_positions=tuple(holdings),
        ))
        ui = PaperTradingUIService(service, runner, corporate_actions_provider=provider)

        def run(date, op, cl):
            return ui.run_accounts(["ACC-PAGE"], date, op, cl, pd.DataFrame())

        # 7/2：正常建立当日状态
        r = run("2026-07-02", {SPLIT_TICKER: 5.40}, {SPLIT_TICKER: 5.40})
        assert "ACC-PAGE" in r["success"]

        # 7/3：pending 涉持仓 → 暂停（页面查询路径驱动）
        r = run("2026-07-03", {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70})
        assert "ACC-PAGE" in r["skipped"] and "待确认" in r["skipped"]["ACC-PAGE"]
        assert service.store.get_nav("ACC-PAGE", "2026-07-03") is None

        # 隔日确认（数据库内 confirm——唯一 confirmed 入口）
        ca_db.confirm_corporate_actions(
            SPLIT_TICKER, "2026-07-03", 2.0, "上交所公告 2026-07-02 登记")
        # 确认后 provider 在隔日必须仍返回该记录（历史 confirmed 不可丢）
        recs_76 = provider("2026-07-06")
        assert recs_76 and any(
            r["ticker"] == SPLIT_TICKER and r.get("status") == "confirmed"
            for r in recs_76), "页面查询隔日丢失历史 confirmed 记录"

        # 7/6：直接运行隔日 → 必须提示先补跑 7/3，不生成交易、不保存状态
        r = run("2026-07-06", {SPLIT_TICKER: 2.72}, {SPLIT_TICKER: 2.72})
        assert "ACC-PAGE" in r["skipped"]
        reason = r["skipped"]["ACC-PAGE"]
        assert "2026-07-03" in reason and "补跑" in reason
        assert "ACC-PAGE" not in r["success"]
        assert service.store.list_trades("ACC-PAGE", trade_date="2026-07-06") == []
        assert service.store.get_nav("ACC-PAGE", "2026-07-06") is None
        assert not service.store.is_day_processed("ACC-PAGE", "2026-07-06")

        # 补跑 7/3（同样走页面查询）→ 正常调整 2000 股 @ 2.70 成本
        r = run("2026-07-03", {SPLIT_TICKER: 2.70}, {SPLIT_TICKER: 2.70})
        assert "ACC-PAGE" in r["success"], f"补跑失败: {r}"
        pos_list = service.store.list_positions("ACC-PAGE", "2026-07-03")
        pos = next(p for p in pos_list if p["ticker"] == SPLIT_TICKER)
        assert pos["shares"] == 2000
        assert pos["cost_price"] == pytest.approx(2.70, abs=1e-9)

        # 再跑 7/6 → 正常继续：无暂停、无错误止损（2.72 vs 2.70 = +0.7%）
        r = run("2026-07-06", {SPLIT_TICKER: 2.72}, {SPLIT_TICKER: 2.72})
        assert "ACC-PAGE" in r["success"], f"恢复运行失败: {r}"
        assert service.store.list_trades("ACC-PAGE", trade_date="2026-07-06") == []
        nav_76 = service.store.get_nav("ACC-PAGE", "2026-07-06")
        assert nav_76 is not None
        assert nav_76["nav"] == pytest.approx(994_600.0 + 2000 * 2.72, abs=0.02)

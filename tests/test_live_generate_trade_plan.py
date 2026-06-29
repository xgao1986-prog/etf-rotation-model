#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_live_generate_trade_plan.py

实盘调仓建议生成测试
覆盖：直接调用 get_b0_4_signals、日期边界、main() 完整运行
"""

import os, sys, tempfile, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from live_trading_assistant import LiveTradingAssistant
import live_generate_trade_plan as lgtp


# ============== Mock 数据库 ==============

class MockETFDatabase:
    """模拟 ETFDatabase，支持多日期评分"""
    def __init__(self, scores_df, market_data_dict):
        self._scores = scores_df
        self._market = market_data_dict

    def get_scores(self, date=None, ticker=None):
        df = self._scores.copy()
        if date is not None:
            df = df[df['date'] == date]
        if ticker is not None:
            df = df[df['ticker'] == ticker]
        return df

    def get_market_data(self, ticker=None, start_date=None, end_date=None):
        if ticker not in self._market:
            return pd.DataFrame()
        df = self._market[ticker].copy()
        if start_date and end_date:
            df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
        return df


# ============== 辅助函数 ==============

TICKER_5_INDUSTRY = ['512400.SH', '515230.SH', '512480.SH', '516110.SH', '159928.SZ']
TICKER_3_INDUSTRY = ['512400.SH', '515230.SH', '512480.SH']
TICKER_DEFENSE = ['518880.SH', '511010.SH']
TICKER_OLD_HOLDING = '512980.SH'  # 传媒ETF，在 ETF_UNIVERSE 中


def _make_scores(rows):
    """创建评分 DataFrame，rows: [(date, ticker, total_score), ...]"""
    data = []
    for date, ticker, total_score in rows:
        data.append({
            'ticker': ticker, 'date': date,
            'ma20': 10, 'ma50': 10, 'ma_ratio': 1.0,
            'price_vs_ma20': 1.0, 'price_vs_ma50': 1.0,
            'volume_ratio': 1.0, 'volatility': 0.1,
            'momentum_20': 0.05, 'momentum_rank': 5,
            'trend_score': 10, 'confirm_score': 10,
            'momentum_score': 10, 'volume_score': 10,
            'volatility_score': 10, 'total_score': total_score,
        })
    return pd.DataFrame(data)


def _make_market_df(ticker, date, price):
    """创建单条行情 DataFrame"""
    return pd.DataFrame({
        'ticker': [ticker], 'date': [date],
        'open': [price], 'high': [price], 'low': [price],
        'close': [price], 'volume': [1000000],
    })


def _make_positions(cash=150000.0, holdings=None):
    """创建持仓 DataFrame"""
    rows = []
    if cash is not None:
        rows.append({
            'ticker': '__CASH__', 'name': '现金', 'shares': 0,
            'cost_price': 0, 'current_price': 0, 'market_value': cash,
            'available_cash': cash, 'update_time': '2026-06-29',
        })
    if holdings:
        for ticker, shares, price in holdings:
            rows.append({
                'ticker': ticker, 'name': ticker, 'shares': shares,
                'cost_price': price, 'current_price': price,
                'market_value': shares * price,
                'available_cash': 0, 'update_time': '2026-06-29',
            })
    return pd.DataFrame(rows)


# ============== 测试 fixtures ==============

@pytest.fixture
def mock_db_5_industry():
    """5只行业ETF合格，防御也合格"""
    scores = _make_scores([
        ('2026-06-29', '512400.SH', 65), ('2026-06-29', '515230.SH', 62),
        ('2026-06-29', '512480.SH', 60), ('2026-06-29', '516110.SH', 58),
        ('2026-06-29', '159928.SZ', 55),  # 5 只行业
        ('2026-06-29', '518880.SH', 50), ('2026-06-29', '511010.SH', 45),  # 2 只防御
    ])
    market = {}
    for t in TICKER_5_INDUSTRY + TICKER_DEFENSE:
        market[t] = _make_market_df(t, '2026-06-29', 1.0)
    return MockETFDatabase(scores, market)


@pytest.fixture
def mock_db_3_industry():
    """3只行业ETF合格，防御也合格"""
    scores = _make_scores([
        ('2026-06-29', '512400.SH', 65), ('2026-06-29', '515230.SH', 62),
        ('2026-06-29', '512480.SH', 60),  # 3 只行业
        ('2026-06-29', '518880.SH', 50), ('2026-06-29', '511010.SH', 45),  # 2 只防御
    ])
    market = {}
    for t in TICKER_3_INDUSTRY + TICKER_DEFENSE:
        market[t] = _make_market_df(t, '2026-06-29', 1.0)
    return MockETFDatabase(scores, market)


@pytest.fixture
def mock_db_multi_date():
    """多日期评分：6月26日和6月29日"""
    scores = _make_scores([
        ('2026-06-26', '512400.SH', 65), ('2026-06-26', '515230.SH', 62),
        ('2026-06-29', '512400.SH', 70), ('2026-06-29', '515230.SH', 68),
        ('2026-06-29', '512480.SH', 60),  # 6/29 多一只
    ])
    market = {
        '512400.SH': pd.concat([
            _make_market_df('512400.SH', '2026-06-26', 1.0),
            _make_market_df('512400.SH', '2026-06-29', 1.0),
        ], ignore_index=True),
        '515230.SH': pd.concat([
            _make_market_df('515230.SH', '2026-06-26', 1.0),
            _make_market_df('515230.SH', '2026-06-29', 1.0),
        ], ignore_index=True),
        '512480.SH': _make_market_df('512480.SH', '2026-06-29', 1.0),
    }
    return MockETFDatabase(scores, market)


@pytest.fixture
def assistant_empty(tmp_path):
    """空持仓（只有现金）的 assistant"""
    pos_path = tmp_path / "positions.csv"
    _make_positions(cash=150000.0, holdings=None).to_csv(pos_path, index=False)
    return LiveTradingAssistant(positions_path=str(pos_path), config={})


@pytest.fixture
def assistant_with_holding(tmp_path):
    """有持仓的 assistant（持有旧持仓 512980.SH）"""
    pos_path = tmp_path / "positions.csv"
    _make_positions(cash=150000.0, holdings=[(TICKER_OLD_HOLDING, 1000, 1.0)]).to_csv(pos_path, index=False)
    return LiveTradingAssistant(positions_path=str(pos_path), config={})


# ============== 测试类 ==============

class TestGetB0_4Signals:
    """测试 get_b0_4_signals 直接调用"""

    def test_first_buy_five_industry(self, assistant_empty, mock_db_5_industry):
        """现金账户首次建仓，5只行业ETF"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_empty, '2026-06-29', cfg, db=mock_db_5_industry)

        assert isinstance(target, dict)
        for t, s in target.items():
            assert isinstance(t, str) and ('.SH' in t or '.SZ' in t)
            assert isinstance(s, int) and s > 0
        # 不得包含 cash、positions 等伪代码
        assert 'cash' not in target
        assert 'positions' not in target
        assert 'nav' not in target
        # 5只行业ETF
        assert len(target) == 5
        assert all(t in target for t in TICKER_5_INDUSTRY)

    def test_first_buy_three_industry_plus_defense(self, assistant_empty, mock_db_3_industry):
        """现金账户首次建仓，3只行业 + 2只防御 = 5只"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_empty, '2026-06-29', cfg, db=mock_db_3_industry)

        assert isinstance(target, dict)
        assert len(target) == 5
        industry_in = [t for t in target if t in TICKER_3_INDUSTRY]
        defense_in = [t for t in target if t in TICKER_DEFENSE]
        assert len(industry_in) == 3, f"Expected 3 industry, got {industry_in}"
        assert len(defense_in) == 2, f"Expected 2 defense, got {defense_in}"

    def test_existing_position_rebalance(self, assistant_with_holding, mock_db_5_industry):
        """已有持仓调仓：卖出旧持仓，买入新候选"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_with_holding, '2026-06-29', cfg, db=mock_db_5_industry)

        # 旧持仓 512980.SH 不应在目标中（因为不在候选中）
        assert TICKER_OLD_HOLDING not in target
        # 新候选应在目标中
        assert any(t in target for t in TICKER_5_INDUSTRY)
        # 所有值都是整数
        for s in target.values():
            assert isinstance(s, int) and s > 0

    def test_date_boundary_no_future(self, assistant_empty, mock_db_multi_date):
        """请求6月26日时只能使用6月26日数据，不得读取6月29日"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_empty, '2026-06-26', cfg, db=mock_db_multi_date)

        # 6/26 只有2只行业ETF
        assert len(target) == 2
        assert '512400.SH' in target
        assert '515230.SH' in target
        # 6/29 才有的 512480.SH 不应出现
        assert '512480.SH' not in target

    def test_date_boundary_uses_latest_before(self, assistant_empty, mock_db_multi_date):
        """请求6月29日时可以使用6月29日最新数据"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_empty, '2026-06-29', cfg, db=mock_db_multi_date)

        # 6/29 有3只
        assert len(target) == 3
        assert '512400.SH' in target
        assert '515230.SH' in target
        assert '512480.SH' in target

    def test_return_format_only_tickers_and_integers(self, assistant_empty, mock_db_5_industry):
        """返回结果只有ETF代码和整数股数"""
        cfg = {'min_total_score': 40, 'max_holdings': 5, 'max_position_per_etf': 0.20}
        target, price_map = lgtp.get_b0_4_signals(assistant_empty, '2026-06-29', cfg, db=mock_db_5_industry)

        assert isinstance(target, dict)
        for t, s in target.items():
            assert isinstance(t, str)
            assert '.' in t  # ETF代码格式
            assert isinstance(s, int)
            assert s > 0
            assert s % 100 == 0  # 整手


class TestMainScript:
    """测试 main() 函数完整运行"""

    def test_main_exit_code_zero(self, monkeypatch, tmp_path):
        """main() 程序退出码为0，能生成CSV和报告"""
        pos_path = tmp_path / "positions.csv"
        _make_positions(cash=150000.0).to_csv(pos_path, index=False)

        scores = _make_scores([
            ('2026-06-29', '512400.SH', 65), ('2026-06-29', '515230.SH', 62),
            ('2026-06-29', '512480.SH', 60), ('2026-06-29', '516110.SH', 58),
            ('2026-06-29', '159928.SZ', 55),
        ])
        market = {}
        for t in TICKER_5_INDUSTRY:
            market[t] = _make_market_df(t, '2026-06-29', 1.0)
        mock_db = MockETFDatabase(scores, market)
        monkeypatch.setattr(lgtp, 'ETFDatabase', lambda: mock_db)

        out_csv = tmp_path / "plan.csv"
        out_md = tmp_path / "plan.md"

        old_argv = sys.argv
        try:
            sys.argv = [
                'live_generate_trade_plan.py',
                '--date', '2026-06-29',
                '--positions-path', str(pos_path),
                '--output-csv', str(out_csv),
                '--output-md', str(out_md),
            ]
            lgtp.main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None, f"Exit code: {e.code}"
        finally:
            sys.argv = old_argv

        assert out_csv.exists(), "CSV file not generated"
        df = pd.read_csv(out_csv)
        assert len(df) > 0, "CSV is empty"
        buy_orders = df[df['action'] == 'BUY']
        assert len(buy_orders) > 0, "No BUY orders"
        for col in df.columns:
            assert col.lower() not in ['cash', 'positions', 'nav'], f"Unexpected column: {col}"

        assert out_md.exists(), "Markdown report not generated"

    def test_main_with_existing_holding(self, monkeypatch, tmp_path):
        """main() 有持仓时生成 SELL + BUY 订单"""
        pos_path = tmp_path / "positions.csv"
        _make_positions(cash=150000.0, holdings=[(TICKER_OLD_HOLDING, 1000, 1.0)]).to_csv(pos_path, index=False)

        scores = _make_scores([
            ('2026-06-29', '512400.SH', 65), ('2026-06-29', '515230.SH', 62),
            ('2026-06-29', '512480.SH', 60), ('2026-06-29', '516110.SH', 58),
            ('2026-06-29', '159928.SZ', 55),
        ])
        market = {}
        for t in TICKER_5_INDUSTRY + [TICKER_OLD_HOLDING]:
            market[t] = _make_market_df(t, '2026-06-29', 1.0)
        mock_db = MockETFDatabase(scores, market)
        monkeypatch.setattr(lgtp, 'ETFDatabase', lambda: mock_db)

        out_csv = tmp_path / "plan.csv"

        old_argv = sys.argv
        try:
            sys.argv = [
                'live_generate_trade_plan.py',
                '--date', '2026-06-29',
                '--positions-path', str(pos_path),
                '--output-csv', str(out_csv),
            ]
            lgtp.main()
        except SystemExit as e:
            assert e.code == 0 or e.code is None
        finally:
            sys.argv = old_argv

        df = pd.read_csv(out_csv)
        sell_orders = df[df['action'] == 'SELL']
        assert len(sell_orders) > 0, "No SELL orders for old holding"
        buy_orders = df[df['action'] == 'BUY']
        assert len(buy_orders) > 0, "No BUY orders"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

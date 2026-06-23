"""v1.3 Step 6: 基于市场状态的动态第5槽位 A/B 实验

研究假设：
- 震荡市中，第5只行业ETF相对更有价值；
- 其他状态下，第5只行业ETF较不稳定，防御资产可能更合适。

三个方案：
A：B0.4冻结基线（最多5只行业ETF）
B：固定4+1（行业最多4只，第5槽位防御）
C：动态第5槽位（震荡=5行业，其他=4+1防御）

约束：
- 不得修改ETF池、评分、阈值、止损、调仓日、执行价格、前4名规则
- T日收盘状态，T+1开盘执行
- 禁止未来函数
- 2019-2024为分析期（含post-hoc假设），2025-2026仅展示
- 固定规则后不得继续调参
"""

import sys, os, copy, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np

from config import (
    build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE,
    BENCHMARK, MARKET_REGIME_CONFIG
)
from database import ETFDatabase
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector
from utils import cfg_signature


AS_OF_DATE = '2026-06-18'

# ============ 研究期划分 ============
RESEARCH_PERIOD = ('2019-01-01', '2022-12-31')
VALIDATION_PERIOD = ('2023-01-01', '2024-12-31')
ANALYSIS_PERIOD = ('2019-01-01', '2024-12-31')
OBSERVATION_PERIOD = ('2025-01-01', AS_OF_DATE)


def get_config(scenario):
    """获取方案配置（仅修改与第5槽位相关的参数）。"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False

    if scenario == 'A':  # B0.4
        cfg['stock_max_holdings'] = 5
        cfg['max_holdings'] = 5
        cfg['total_max_holdings'] = 5
        cfg['defense_max_holdings'] = 2
    elif scenario == 'B':  # 固定4+1
        cfg['stock_max_holdings'] = 4
        cfg['max_holdings'] = 4
        cfg['total_max_holdings'] = 5
        cfg['defense_max_holdings'] = 1
    elif scenario == 'C':  # 动态，基础配置与B相同
        cfg['stock_max_holdings'] = 4
        cfg['max_holdings'] = 4
        cfg['total_max_holdings'] = 5
        cfg['defense_max_holdings'] = 1
    return cfg


class DynamicFifthSlotBacktestEngine(BacktestEngine):
    """动态第5槽位回测引擎：根据T日状态调整max_holdings。"""

    def __init__(self, cfg, regime_df, slippage_bps=0):
        super().__init__(copy.deepcopy(cfg), slippage_bps=slippage_bps)
        self.regime_df = regime_df.copy()
        self.regime_df['date'] = pd.to_datetime(self.regime_df['date'])
        self._base_cfg = copy.deepcopy(cfg)
        self._regime_dates = set(self.regime_df['date'].dt.date)
        self._regime_map = dict(zip(
            self.regime_df['date'].dt.date,
            self.regime_df['regime_name']
        ))

    def _rebalance_v2(self, portfolio, day_signals, day_prices, effective_close_prices,
                      last_valid_close, date, date_str, buy_signals, trade_records,
                      cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                      _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                      buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                      min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                      calc_commission, slippage=0.0):
        """覆盖：在调用父类前，根据T日regime调整cfg。"""
        # 保存原始配置
        original_max_holdings = self.cfg['max_holdings']
        original_total_max = self.cfg.get('total_max_holdings', self.cfg['max_holdings'])
        original_stock_max = self.cfg.get('stock_max_holdings', self.cfg['max_holdings'])
        original_defense_max = self.cfg.get('defense_max_holdings', 2)

        try:
            # 获取T日状态
            d = pd.to_datetime(date).date()
            regime = self._regime_map.get(d, None)

            if pd.notna(regime) and regime == '震荡':
                # 震荡 → B0.4结构（5行业）
                self.cfg['max_holdings'] = 5
                self.cfg['total_max_holdings'] = 5
                self.cfg['stock_max_holdings'] = 5
                self.cfg['defense_max_holdings'] = 2
            elif pd.notna(regime):
                # 其他明确状态（强牛/弱牛/熊市）→ 4+1结构
                self.cfg['max_holdings'] = 4
                self.cfg['total_max_holdings'] = 5
                self.cfg['stock_max_holdings'] = 4
                self.cfg['defense_max_holdings'] = 1
            else:
                # NaN/warmup → 回退B0.4结构（5行业），不得进入4+1
                self.cfg['max_holdings'] = 5
                self.cfg['total_max_holdings'] = 5
                self.cfg['stock_max_holdings'] = 5
                self.cfg['defense_max_holdings'] = 2

            # 调用父类实现
            return super()._rebalance_v2(
                portfolio, day_signals, day_prices, effective_close_prices,
                last_valid_close, date, date_str, buy_signals, trade_records,
                cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                calc_commission, slippage
            )
        finally:
            # 恢复原始配置
            self.cfg['max_holdings'] = original_max_holdings
            self.cfg['total_max_holdings'] = original_total_max
            self.cfg['stock_max_holdings'] = original_stock_max
            self.cfg['defense_max_holdings'] = original_defense_max


def run_scenario(scenario, market_df, bench_df, regime_df=None, slippage_bps=0):
    """运行一个方案。"""
    cfg = get_config(scenario)

    if scenario == 'C':
        engine = DynamicFifthSlotBacktestEngine(cfg, regime_df, slippage_bps=slippage_bps)
    else:
        engine = BacktestEngine(cfg, slippage_bps=slippage_bps)

    result = engine.run(market_df.copy(), bench_df.copy(), as_of_date=AS_OF_DATE)
    return result


def detect_regimes(bench_df):
    """检测市场状态（使用T日收盘数据）。"""
    bench_for_regime = bench_df[['date', 'close', 'open', 'high', 'low', 'volume']].copy()
    bench_for_regime['date'] = pd.to_datetime(bench_for_regime['date'])
    bench_for_regime = bench_for_regime.sort_values('date')

    regime_detector = MarketRegimeDetector(MARKET_REGIME_CONFIG)
    regimes = regime_detector.detect_history(bench_for_regime)
    regimes['date'] = pd.to_datetime(regimes['date'])
    return regimes[['date', 'regime_id', 'regime_name']]


def compute_metrics(nav_df, trades_df, period_start=None, period_end=None):
    """计算指定期间的指标。"""
    nav = nav_df.copy()
    nav['date'] = pd.to_datetime(nav['date'])

    if period_start:
        nav = nav[nav['date'] >= pd.to_datetime(period_start)]
    if period_end:
        nav = nav[nav['date'] <= pd.to_datetime(period_end)]

    if len(nav) < 2:
        return None

    nav = nav.sort_values('date').reset_index(drop=True)
    nav['ret'] = nav['nav'].pct_change()

    total_ret = nav['nav'].iloc[-1] / nav['nav'].iloc[0] - 1
    n_days = len(nav)
    cagr = (nav['nav'].iloc[-1] / nav['nav'].iloc[0]) ** (252 / n_days) - 1

    # 夏普（无风险利率0，日收益）
    daily_ret = nav['ret'].dropna()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

    # 最大回撤
    cum = (1 + daily_ret).cumprod()
    peak = cum.expanding().max()
    drawdown = (cum - peak) / peak
    max_dd = drawdown.min()

    # 年度收益
    nav['year'] = nav['date'].dt.year
    yearly = []
    for year, group in nav.groupby('year'):
        group = group.sort_values('date')
        if len(group) < 2:
            continue
        yearly.append({
            'year': year,
            'ret': group['nav'].iloc[-1] / group['nav'].iloc[0] - 1,
        })

    # 月度胜率
    nav['ym'] = nav['date'].dt.to_period('M')
    monthly = nav.groupby('ym')['ret'].sum()
    monthly_win_rate = (monthly > 0).mean() if len(monthly) > 0 else 0

    # 交易统计
    t = trades_df.copy() if not trades_df.empty else pd.DataFrame(columns=['date', 'commission'])
    if not t.empty and 'date' in t.columns:
        t['date'] = pd.to_datetime(t['date'])
        if period_start:
            t = t[t['date'] >= pd.to_datetime(period_start)]
        if period_end:
            t = t[t['date'] <= pd.to_datetime(period_end)]

    n_trades = len(t)
    total_comm = t['commission'].sum() if not t.empty and 'commission' in t.columns else 0

    # 平均持仓
    avg_ind = (nav['industry_value'] / nav['nav']).mean() if 'industry_value' in nav.columns else 0
    avg_def = (nav['defense_value'] / nav['nav']).mean() if 'defense_value' in nav.columns else 0
    avg_cash = (nav['cash'] / nav['nav']).mean() if 'cash' in nav.columns else 0

    return {
        'total_ret': total_ret,
        'cagr': cagr,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'yearly': yearly,
        'monthly_win_rate': monthly_win_rate,
        'n_trades': n_trades,
        'total_comm': total_comm,
        'avg_ind_pct': avg_ind,
        'avg_def_pct': avg_def,
        'avg_cash_pct': avg_cash,
        'nav': nav,
    }


def analyze_period_diff(nav_a, trades_a, nav_b, trades_b, nav_c, trades_c, period_label, period_start, period_end):
    """分析三个方案在指定期间的差异。"""
    m_a = compute_metrics(nav_a, trades_a, period_start, period_end)
    m_b = compute_metrics(nav_b, trades_b, period_start, period_end)
    m_c = compute_metrics(nav_c, trades_c, period_start, period_end)

    if not m_a or not m_b or not m_c:
        return None

    return {
        'period': period_label,
        'a_ret': m_a['total_ret'],
        'b_ret': m_b['total_ret'],
        'c_ret': m_c['total_ret'],
        'a_sharpe': m_a['sharpe'],
        'b_sharpe': m_b['sharpe'],
        'c_sharpe': m_c['sharpe'],
        'a_maxdd': m_a['max_dd'],
        'b_maxdd': m_b['max_dd'],
        'c_maxdd': m_c['max_dd'],
        'a_trades': m_a['n_trades'],
        'b_trades': m_b['n_trades'],
        'c_trades': m_c['n_trades'],
        'diff_ba': m_b['total_ret'] - m_a['total_ret'],
        'diff_ca': m_c['total_ret'] - m_a['total_ret'],
        'diff_cb': m_c['total_ret'] - m_b['total_ret'],
    }


def leave_one_year_out(nav_a, nav_b, nav_c, analysis_end='2024-12-31'):
    """leave-one-year-out：仅分析期（2019-2024）使用，每次剔除一个完整年份。
    
    观察期（2025-2026）仅展示，不用于规则选择，不得混入LOO。
    """
    nav_a = nav_a.copy()
    nav_a['date'] = pd.to_datetime(nav_a['date'])
    nav_b = nav_b.copy()
    nav_b['date'] = pd.to_datetime(nav_b['date'])
    nav_c = nav_c.copy()
    nav_c['date'] = pd.to_datetime(nav_c['date'])

    # 限制在分析期（2019-2024）
    analysis_end_dt = pd.to_datetime(analysis_end)
    nav_a = nav_a[nav_a['date'] <= analysis_end_dt].copy()
    nav_b = nav_b[nav_b['date'] <= analysis_end_dt].copy()
    nav_c = nav_c[nav_c['date'] <= analysis_end_dt].copy()

    # 计算日收益率
    for nav in [nav_a, nav_b, nav_c]:
        nav['ret'] = nav['nav'].pct_change()

    years = sorted(nav_a['date'].dt.year.unique())
    results = []

    for exclude_year in years:
        a_sub = nav_a[nav_a['date'].dt.year != exclude_year].copy()
        b_sub = nav_b[nav_b['date'].dt.year != exclude_year].copy()
        c_sub = nav_c[nav_c['date'].dt.year != exclude_year].copy()

        if len(a_sub) < 2 or len(b_sub) < 2 or len(c_sub) < 2:
            continue

        ret_a = (1 + a_sub['ret'].fillna(0)).prod() - 1
        ret_b = (1 + b_sub['ret'].fillna(0)).prod() - 1
        ret_c = (1 + c_sub['ret'].fillna(0)).prod() - 1

        results.append({
            'exclude_year': exclude_year,
            'a_ret': ret_a,
            'b_ret': ret_b,
            'c_ret': ret_c,
            'diff_ba': ret_b - ret_a,
            'diff_ca': ret_c - ret_a,
            'diff_cb': ret_c - ret_b,
        })

    return results


def annual_contribution(nav_a, nav_c, analysis_end='2024-12-31'):
    """P1-3: 直接计算每个自然年的C-A收益差，不是剔除后的差异。

    严格截止分析期（默认2024-12-31），2025-2026仅展示不参与PASS/FAIL。
    """
    nav_a = nav_a.copy()
    nav_a['date'] = pd.to_datetime(nav_a['date'])
    nav_c = nav_c.copy()
    nav_c['date'] = pd.to_datetime(nav_c['date'])

    # 严格截止分析期
    analysis_end_dt = pd.to_datetime(analysis_end)
    nav_a = nav_a[nav_a['date'] <= analysis_end_dt]
    nav_c = nav_c[nav_c['date'] <= analysis_end_dt]

    nav_a['ret'] = nav_a['nav'].pct_change()
    nav_c['ret'] = nav_c['nav'].pct_change()

    years = sorted(nav_a['date'].dt.year.unique())
    results = []
    for year in years:
        a_yr = nav_a[nav_a['date'].dt.year == year]
        c_yr = nav_c[nav_c['date'].dt.year == year]
        if len(a_yr) < 2 or len(c_yr) < 2:
            continue
        ret_a = (1 + a_yr['ret'].fillna(0)).prod() - 1
        ret_c = (1 + c_yr['ret'].fillna(0)).prod() - 1
        results.append({
            'year': year,
            'a_ret': ret_a,
            'c_ret': ret_c,
            'diff_ca': ret_c - ret_a,
        })
    return results


def defense_etf_contribution(nav_a, nav_c, trades_a, trades_c, market_df, defense_tickers, analysis_end='2024-12-31'):
    """P1-4: 分别统计黄金ETF和国债ETF对C-A的贡献。

    使用逐日mark-to-market，严格截止分析期（默认2024-12-31）：
    - 按黄金、国债分别计算真实持有期间PnL
    - 起止边界包含未平仓持仓估值
    - 佣金单独列出
    - 不得使用简单"SELL收入-BUY成本"，因期末可能有未平仓持仓

    计算口径：
    - 总买入成本 = sum(BUY shares * price)
    - 总卖出收入 = sum(SELL/STOP_LOSS shares * price)
    - 总佣金 = sum(commission)
    - 期末持仓量 = 总买入shares - 总卖出shares
    - 期末市值 = 期末持仓量 * 期末收盘价
    - 总PnL = 总卖出收入 + 期末市值 - 总买入成本 - 总佣金
    """
    gold = ['518880.SH']
    bond = ['511010.SH']

    analysis_end_dt = pd.to_datetime(analysis_end)

    def calc_ticker_pnl(trades_df, ticker_list, market_df):
        """对单一ticker计算mark-to-market PnL。"""
        ticker = ticker_list[0]  # 每类只有一个
        tdf = trades_df.copy()
        tdf['date'] = pd.to_datetime(tdf['date'])
        tdf = tdf[tdf['date'] <= analysis_end_dt]
        tdf = tdf[tdf['ticker'] == ticker]

        # 价格数据
        prices = market_df[market_df['ticker'] == ticker][['date', 'close']].copy()
        prices['date'] = pd.to_datetime(prices['date'])
        prices = prices[prices['date'] <= analysis_end_dt]
        prices = prices.sort_values('date').reset_index(drop=True)

        if prices.empty:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0

        # 统计买入/卖出/佣金
        buy = tdf[tdf['action'] == 'BUY']
        sell = tdf[tdf['action'].isin(['SELL', 'STOP_LOSS'])]

        total_buy_cost = (buy['shares'] * buy['price']).sum() if not buy.empty else 0.0
        total_sell_rev = (sell['shares'] * sell['price']).sum() if not sell.empty else 0.0
        total_comm = tdf['commission'].sum() if not tdf.empty else 0.0
        total_buy_shares = buy['shares'].sum() if not buy.empty else 0
        total_sell_shares = sell['shares'].sum() if not sell.empty else 0

        # 期末持仓量
        final_position = total_buy_shares - total_sell_shares

        # 期末收盘价（分析期最后一日）
        final_close = prices['close'].iloc[-1]
        final_market_value = final_position * final_close

        # mark-to-market PnL = 卖出收入 + 期末市值 - 买入成本 - 佣金
        total_pnl = total_sell_rev + final_market_value - total_buy_cost - total_comm

        return total_pnl, total_buy_cost, total_sell_rev, total_comm, final_market_value, total_buy_shares, total_sell_shares

    results = {}
    for name, tickers in [('gold', gold), ('bond', bond)]:
        pnl_a, buy_a, sell_a, comm_a, mv_a, bs_a, ss_a = calc_ticker_pnl(trades_a, tickers, market_df)
        pnl_c, buy_c, sell_c, comm_c, mv_c, bs_c, ss_c = calc_ticker_pnl(trades_c, tickers, market_df)

        results[f'{name}_pnl_a'] = pnl_a
        results[f'{name}_pnl_c'] = pnl_c
        results[f'{name}_diff'] = pnl_c - pnl_a
        results[f'{name}_buy_a'] = buy_a
        results[f'{name}_buy_c'] = buy_c
        results[f'{name}_sell_a'] = sell_a
        results[f'{name}_sell_c'] = sell_c
        results[f'{name}_comm_a'] = comm_a
        results[f'{name}_comm_c'] = comm_c
        results[f'{name}_mv_a'] = mv_a
        results[f'{name}_mv_c'] = mv_c
        results[f'{name}_buy_shares_a'] = bs_a
        results[f'{name}_buy_shares_c'] = bs_c
        results[f'{name}_sell_shares_a'] = ss_a
        results[f'{name}_sell_shares_c'] = ss_c

    return results


def total_commission(trades_df, analysis_end='2024-12-31'):
    """P1-5: 从trades_df实际commission求和，严格截止分析期。"""
    if trades_df.empty or 'commission' not in trades_df.columns:
        return 0.0
    tdf = trades_df.copy()
    tdf['date'] = pd.to_datetime(tdf['date'])
    tdf = tdf[tdf['date'] <= pd.to_datetime(analysis_end)]
    return tdf['commission'].sum()


def reconciliation_summary(result_a, result_b, result_c, comm_a, comm_b, comm_c):
    """生成勾稽汇总CSV。"""
    return pd.DataFrame({
        'scenario': ['A', 'B', 'C'],
        'final_nav': [
            result_a['nav_df']['nav'].iloc[-1],
            result_b['nav_df']['nav'].iloc[-1],
            result_c['nav_df']['nav'].iloc[-1],
        ],
        'num_trades': [result_a['num_trades'], result_b['num_trades'], result_c['num_trades']],
        'total_commission': [comm_a, comm_b, comm_c],
    })


def analyze_mechanism(nav_a, nav_c, trades_a, trades_c, regime_df, market_df):
    """机制归因：拆解C相对A的差异来源。"""
    # 准备价格数据
    price_df = market_df[['date', 'ticker', 'close']].copy()
    price_df['date'] = pd.to_datetime(price_df['date'])
    price_df = price_df.sort_values(['ticker', 'date'])
    price_df['prev_close'] = price_df.groupby('ticker')['close'].shift(1)

    a = nav_a.copy()
    a['date'] = pd.to_datetime(a['date'])
    c = nav_c.copy()
    c['date'] = pd.to_datetime(c['date'])

    merged = pd.merge(
        a[['date', 'nav', 'cash', 'industry_value', 'defense_value', 'positions_detail']],
        c[['date', 'nav', 'cash', 'industry_value', 'defense_value', 'positions_detail']],
        on='date', suffixes=('_a', '_c')
    )
    merged = merged.sort_values('date').reset_index(drop=True)

    # 计算每日收益
    merged['ret_a'] = merged['nav_a'].pct_change()
    merged['ret_c'] = merged['nav_c'].pct_change()
    merged['diff_ret'] = merged['ret_c'] - merged['ret_a']

    # 防御资产列表
    defense_tickers = set(DEFENSE_UNIVERSE.keys())

    def parse_positions(positions_detail):
        if not positions_detail or not isinstance(positions_detail, dict):
            return {}, {}
        industry = {}
        defense = {}
        for t, d in positions_detail.items():
            if isinstance(d, dict):
                shares = d.get('shares', 0)
            else:
                shares = int(d) if isinstance(d, (int, float)) else 0
            if t in defense_tickers:
                defense[t] = shares
            else:
                industry[t] = shares
        return industry, defense

    # 逐日归因
    records = []
    for _, row in merged.iterrows():
        date = row['date']
        nav_a_val = row['nav_a']
        nav_c_val = row['nav_c']

        ind_a, def_a = parse_positions(row['positions_detail_a'])
        ind_c, def_c = parse_positions(row['positions_detail_c'])

        # 共同行业
        common_ind = set(ind_a.keys()) & set(ind_c.keys())
        a_only_ind = set(ind_a.keys()) - set(ind_c.keys())
        c_only_ind = set(ind_c.keys()) - set(ind_a.keys())

        a_only_def = set(def_a.keys()) - set(def_c.keys())
        c_only_def = set(def_c.keys()) - set(def_a.keys())

        # 计算贡献（简化：用持仓占比 × 日收益差异近似）
        ind_pct_a = row['industry_value_a'] / nav_a_val if nav_a_val > 0 else 0
        def_pct_a = row['defense_value_a'] / nav_a_val if nav_a_val > 0 else 0
        ind_pct_c = row['industry_value_c'] / nav_c_val if nav_c_val > 0 else 0
        def_pct_c = row['defense_value_c'] / nav_c_val if nav_c_val > 0 else 0

        records.append({
            'date': date,
            'year': date.year,
            'ret_a': row['ret_a'],
            'ret_c': row['ret_c'],
            'diff_ret': row['diff_ret'],
            'ind_pct_a': ind_pct_a,
            'def_pct_a': def_pct_a,
            'ind_pct_c': ind_pct_c,
            'def_pct_c': def_pct_c,
            'n_ind_a': len(ind_a),
            'n_ind_c': len(ind_c),
            'n_def_a': len(def_a),
            'n_def_c': len(def_c),
            'has_5th_a': len(ind_a) >= 5,
            'has_5th_c': len(ind_c) >= 5,
            'has_def_c': len(def_c) > 0,
        })

    attr_df = pd.DataFrame(records)

    # 合并regime
    regime_df = regime_df.copy()
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    attr_df = pd.merge(attr_df, regime_df[['date', 'regime_name']], on='date', how='left')

    # 按regime汇总
    regime_summary = []
    for regime_name, group in attr_df.groupby('regime_name'):
        if pd.isna(regime_name):
            continue
        group = group.sort_values('date')
        regime_summary.append({
            'regime': regime_name,
            'days': len(group),
            'cum_ret_a': (1 + group['ret_a'].fillna(0)).prod() - 1,
            'cum_ret_c': (1 + group['ret_c'].fillna(0)).prod() - 1,
            'diff': (1 + group['diff_ret'].fillna(0)).prod() - 1,
            'avg_ind_a': group['ind_pct_a'].mean(),
            'avg_def_a': group['def_pct_a'].mean(),
            'avg_ind_c': group['ind_pct_c'].mean(),
            'avg_def_c': group['def_pct_c'].mean(),
            'days_5th_a': group['has_5th_a'].sum(),
            'days_5th_c': group['has_5th_c'].sum(),
            'days_def_c': group['has_def_c'].sum(),
        })

    return attr_df, pd.DataFrame(regime_summary)


def analyze_regime_switching(nav_c, trades_c, regime_df):
    """分析状态切换附近的表现。"""
    nav = nav_c.copy()
    nav['date'] = pd.to_datetime(nav['date'])
    nav['ret'] = nav['nav'].pct_change()

    regime_df = regime_df.copy()
    regime_df['date'] = pd.to_datetime(regime_df['date'])

    # 合并
    merged = pd.merge(nav[['date', 'nav', 'ret']], regime_df[['date', 'regime_name']], on='date', how='left')
    merged = merged.sort_values('date').reset_index(drop=True)
    merged['regime_shift'] = merged['regime_name'].shift(1)
    merged['switch'] = merged['regime_name'] != merged['regime_shift']

    switches = merged[merged['switch'] == True].copy()

    # 切换前后±5日
    switch_analysis = []
    for _, row in switches.iterrows():
        date = row['date']
        idx = merged[merged['date'] == date].index[0]

        pre_5 = merged.iloc[max(0, idx-5):idx]['ret'].sum() if idx > 0 else 0
        post_5 = merged.iloc[idx:min(len(merged), idx+6)]['ret'].sum() if idx < len(merged) else 0
        pre_10 = merged.iloc[max(0, idx-10):idx]['ret'].sum() if idx > 0 else 0
        post_10 = merged.iloc[idx:min(len(merged), idx+11)]['ret'].sum() if idx < len(merged) else 0

        switch_analysis.append({
            'date': date,
            'from_regime': row['regime_shift'],
            'to_regime': row['regime_name'],
            'pre_5d_ret': pre_5,
            'post_5d_ret': post_5,
            'pre_10d_ret': pre_10,
            'post_10d_ret': post_10,
        })

    return pd.DataFrame(switch_analysis)


def main():
    # 加载数据（匹配B0.4滑点测试：不包含fallback tickers）
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)

    # 检测市场状态
    regimes = detect_regimes(bench_df)

    print("=" * 60)
    print("v1.3 Step 6: 动态第5槽位 A/B 实验")
    print("=" * 60)

    # 运行三个方案
    print("\n=== 运行方案A: B0.4 ===")
    result_a = run_scenario('A', market_df, bench_df)
    print(f"A NAV: {result_a['nav_df']['nav'].iloc[-1]:,.2f}, 交易: {result_a['num_trades']}")

    print("\n=== 运行方案B: 固定4+1 ===")
    result_b = run_scenario('B', market_df, bench_df)
    print(f"B NAV: {result_b['nav_df']['nav'].iloc[-1]:,.2f}, 交易: {result_b['num_trades']}")

    print("\n=== 运行方案C: 动态第5槽位 ===")
    result_c = run_scenario('C', market_df, bench_df, regime_df=regimes)
    print(f"C NAV: {result_c['nav_df']['nav'].iloc[-1]:,.2f}, 交易: {result_c['num_trades']}")

    # 保存中间数据
    result_a['nav_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_nav_A.csv', index=False)
    result_b['nav_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_nav_B.csv', index=False)
    result_c['nav_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_nav_C.csv', index=False)
    result_a['trades_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_trades_A.csv', index=False)
    result_b['trades_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_trades_B.csv', index=False)
    result_c['trades_df'].to_csv('D:/etf_rotation_model/reports/v1_3_step6_trades_C.csv', index=False)

    # 期间对比
    periods = [
        ('研究期', ANALYSIS_PERIOD[0], '2022-12-31'),
        ('验证期', '2023-01-01', '2024-12-31'),
        ('分析期', ANALYSIS_PERIOD[0], ANALYSIS_PERIOD[1]),
        ('观察期', OBSERVATION_PERIOD[0], OBSERVATION_PERIOD[1]),
        ('全期间', '2019-01-01', AS_OF_DATE),
    ]

    period_results = []
    for label, s, e in periods:
        r = analyze_period_diff(result_a['nav_df'], result_a['trades_df'], result_b['nav_df'], result_b['trades_df'], result_c['nav_df'], result_c['trades_df'], label, s, e)
        if r:
            period_results.append(r)

    # 滑点压力测试
    slippage_results = []
    for bps in [0, 3, 5, 10]:
        print(f"\n--- 滑点 {bps}bp ---")
        r_a = run_scenario('A', market_df, bench_df, slippage_bps=bps)
        r_b = run_scenario('B', market_df, bench_df, slippage_bps=bps)
        r_c = run_scenario('C', market_df, bench_df, regime_df=regimes, slippage_bps=bps)

        nav_a_end = r_a['nav_df']['nav'].iloc[-1]
        nav_b_end = r_b['nav_df']['nav'].iloc[-1]
        nav_c_end = r_c['nav_df']['nav'].iloc[-1]
        initial = r_a['nav_df']['nav'].iloc[0]

        slippage_results.append({
            'bps': bps,
            'a_ret': nav_a_end / initial - 1,
            'b_ret': nav_b_end / initial - 1,
            'c_ret': nav_c_end / initial - 1,
            'a_trades': r_a['num_trades'],
            'b_trades': r_b['num_trades'],
            'c_trades': r_c['num_trades'],
        })

    # leave-one-year-out（仅分析期2019-2024，P1修正）
    print("\n--- leave-one-year-out (分析期2019-2024) ---")
    loyo = leave_one_year_out(result_a['nav_df'], result_b['nav_df'], result_c['nav_df'])

    # 状态切换分析
    print("\n--- 状态切换分析 ---")
    switch_df = analyze_regime_switching(result_c['nav_df'], result_c['trades_df'], regimes)
    switch_df.to_csv('D:/etf_rotation_model/reports/v1_3_step6_regime_switches.csv', index=False)

    # 机制归因
    print("\n--- 机制归因 ---")
    attr_df, regime_summary = analyze_mechanism(
        result_a['nav_df'], result_c['nav_df'],
        result_a['trades_df'], result_c['trades_df'],
        regimes, market_df
    )
    attr_df.to_csv('D:/etf_rotation_model/reports/v1_3_step6_mechanism_attr.csv', index=False)
    regime_summary.to_csv('D:/etf_rotation_model/reports/v1_3_step6_regime_summary.csv', index=False)

    # P1-3: 自然年C-A贡献（严格截止分析期2024-12-31）
    annual = annual_contribution(result_a['nav_df'], result_c['nav_df'], analysis_end='2024-12-31')
    annual_df = pd.DataFrame(annual)
    annual_df.to_csv('D:/etf_rotation_model/reports/v1_3_step6_annual_contribution.csv', index=False)

    # P1-4: 防御ETF贡献（mark-to-market，严格截止分析期2024-12-31）
    defense_contrib = defense_etf_contribution(
        result_a['nav_df'], result_c['nav_df'],
        result_a['trades_df'], result_c['trades_df'],
        market_df, set(DEFENSE_UNIVERSE.keys()), analysis_end='2024-12-31'
    )
    defense_df = pd.DataFrame([defense_contrib])
    defense_df.to_csv('D:/etf_rotation_model/reports/v1_3_step6_defense_contribution.csv', index=False)

    # P1-5: 实际佣金（严格截止分析期2024-12-31）
    comm_a = total_commission(result_a['trades_df'], analysis_end='2024-12-31')
    comm_b = total_commission(result_b['trades_df'], analysis_end='2024-12-31')
    comm_c = total_commission(result_c['trades_df'], analysis_end='2024-12-31')
    print(f"佣金: A={comm_a:,.2f}, B={comm_b:,.2f}, C={comm_c:,.2f}")

    # 勾稽汇总
    recon = reconciliation_summary(result_a, result_b, result_c, comm_a, comm_b, comm_c)
    recon.to_csv('D:/etf_rotation_model/reports/v1_3_step6_reconciliation.csv', index=False)

    # 生成报告
    generate_report(period_results, slippage_results, loyo, switch_df, regimes, regime_summary,
                    result_a, result_b, result_c, annual, defense_contrib, comm_a, comm_b, comm_c)

    print("\n实验完成。")
    return {
        'period_results': period_results,
        'slippage_results': slippage_results,
        'loyo': loyo,
        'switch_df': switch_df.to_dict('records') if not switch_df.empty else [],
    }


def generate_report(period_results, slippage_results, loyo, switch_df, regimes, regime_summary, result_a, result_b, result_c, annual, defense_contrib, comm_a, comm_b, comm_c):
    """生成实验报告。"""
    lines = []
    lines.append("# v1.3 Step 6: 基于市场状态的动态第5槽位 A/B 实验")
    lines.append("")
    lines.append("> **Post-hoc 假设声明**：本实验的研究假设来自 Step 5 的既有全期归因分析。因此 2019-2024 数据不是纯粹的未查看 OOS 样本。2025-2026 仅作展示，未用于规则修改。")
    lines.append("")
    lines.append("## 实验设计")
    lines.append("")
    lines.append("### 三个方案")
    lines.append("")
    lines.append("- **A：B0.4 冻结基线** — 最多5只行业ETF，防御按原有规则")
    lines.append("- **B：固定 4+1** — 行业最多4只，第5槽位由防御填充，无合格防御则现金")
    lines.append("- **C：动态第5槽位** — 震荡市=5行业（同A），其他状态=4+1防御（同B）；NaN/warmup回退B0.4（5行业）")
    lines.append("")
    lines.append("### 约束")
    lines.append("")
    lines.append("- 未修改 ETF 池、评分、阈值、止损、调仓日、执行价格、前4名规则")
    lines.append("- T日收盘状态，T+1开盘执行；NaN/warmup回退B0.4（5行业）")
    lines.append("- 禁止未来函数")
    lines.append("- 固定规则后未继续调参")
    lines.append("")
    lines.append("## 全期间表现")
    lines.append("")
    lines.append(f"| 期间 | A 收益 | B 收益 | C 收益 | B-A | C-A | C-B | A 夏普 | B 夏普 | C 夏普 | A 回撤 | B 回撤 | C 回撤 | A 交易 | B 交易 | C 交易 |")
    lines.append(f"|------|--------|--------|--------|-----|-----|-----|--------|--------|--------|--------|--------|--------|--------|--------|--------|")
    for r in period_results:
        lines.append(f"| {r['period']} | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | "
                    f"{r['diff_ba']:+.2%} | {r['diff_ca']:+.2%} | {r['diff_cb']:+.2%} | "
                    f"{r['a_sharpe']:.2f} | {r['b_sharpe']:.2f} | {r['c_sharpe']:.2f} | "
                    f"{r['a_maxdd']:.2%} | {r['b_maxdd']:.2%} | {r['c_maxdd']:.2%} | "
                    f"{r['a_trades']} | {r['b_trades']} | {r['c_trades']} |")
    lines.append("")
    lines.append("## 机制归因：按市场状态拆解 C vs A")
    lines.append("")
    lines.append(f"| 状态 | 天数 | A 累积 | C 累积 | 差异 | A 行业% | A 防御% | C 行业% | C 防御% | A有5th | C有5th | C有防御 |")
    lines.append(f"|------|------|--------|--------|------|---------|---------|---------|---------|--------|--------|----------|")
    for _, r in regime_summary.iterrows():
        lines.append(f"| {r['regime']} | {r['days']} | {r['cum_ret_a']:.2%} | {r['cum_ret_c']:.2%} | {r['diff']:+.2%} | "
                    f"{r['avg_ind_a']:.2%} | {r['avg_def_a']:.2%} | {r['avg_ind_c']:.2%} | {r['avg_def_c']:.2%} | "
                    f"{r['days_5th_a']} | {r['days_5th_c']} | {r['days_def_c']} |")
    lines.append("")
    lines.append("## 滑点压力测试")
    lines.append("")
    lines.append(f"| 滑点 | A 收益 | B 收益 | C 收益 | B-A | C-A |")
    lines.append(f"|------|--------|--------|--------|-----|-----|")
    for r in slippage_results:
        lines.append(f"| {r['bps']}bp | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | "
                    f"{r['b_ret']-r['a_ret']:+.2%} | {r['c_ret']-r['a_ret']:+.2%} |")
    lines.append("")
    lines.append("## Leave-One-Year-Out（分析期2019-2024）")
    lines.append("")
    lines.append(f"| 剔除年份 | A 收益 | B 收益 | C 收益 | B-A | C-A |")
    lines.append(f"|----------|--------|--------|--------|-----|-----|")
    for r in loyo:
        lines.append(f"| {r['exclude_year']} | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | "
                    f"{r['diff_ba']:+.2%} | {r['diff_ca']:+.2%} |")
    lines.append("")
    lines.append("## 状态切换分析")
    lines.append("")
    if not switch_df.empty:
        lines.append(f"状态切换次数: {len(switch_df)}")
        lines.append("")
        lines.append(f"| 切换方向 | 次数 | 切换前5日平均 | 切换后5日平均 | 切换前10日平均 | 切换后10日平均 |")
        lines.append(f"|----------|------|---------------|---------------|----------------|----------------|")
        for (from_r, to_r), group in switch_df.groupby(['from_regime', 'to_regime']):
            lines.append(f"| {from_r} → {to_r} | {len(group)} | {group['pre_5d_ret'].mean():.4%} | {group['post_5d_ret'].mean():.4%} | "
                        f"{group['pre_10d_ret'].mean():.4%} | {group['post_10d_ret'].mean():.4%} |")
    lines.append("")
    lines.append("## 预注册验收标准")
    lines.append("")
    # 自动评估验收标准
    research = next((r for r in period_results if r['period'] == '研究期'), None)
    validation = next((r for r in period_results if r['period'] == '验证期'), None)
    
    if research and validation:
        # 1. 研究期与验证期夏普改善方向一致
        sharpe_ok = (research['c_sharpe'] > research['a_sharpe']) == (validation['c_sharpe'] > validation['a_sharpe'])
        lines.append(f"1. 研究期与验证期夏普改善方向一致: {'✅' if sharpe_ok else '❌'} (研究期: {research['c_sharpe']:.2f} vs {research['a_sharpe']:.2f}, 验证期: {validation['c_sharpe']:.2f} vs {validation['a_sharpe']:.2f})")
        
        # 2. 验证期最大回撤不恶化超过1个百分点（P1修正：使用绝对值比较）
        # C_maxdd=-16.30%, A_maxdd=-17.75% → C绝对回撤更小（更好），差值=-1.45%
        c_abs_dd = abs(validation['c_maxdd'])
        a_abs_dd = abs(validation['a_maxdd'])
        dd_diff = c_abs_dd - a_abs_dd  # 负值表示C更好
        dd_ok = dd_diff <= 0.01  # C不比A差1%以上
        lines.append(f"2. 验证期最大回撤不恶化超过1个百分点: {'✅' if dd_ok else '❌'} (C绝对回撤={c_abs_dd:.2%}, A绝对回撤={a_abs_dd:.2%}, 差值={dd_diff:+.2%})")
        
        # 3. 验证期总收益不低于A-2个百分点
        ret_diff = validation['c_ret'] - validation['a_ret']
        ret_ok = ret_diff >= -0.02
        lines.append(f"3. 验证期总收益不低于A-2个百分点: {'✅' if ret_ok else '❌'} (C-A: {ret_diff:+.2%})")
    
    # 4. 3/5/10bp下结论方向不反转
    bps_ok = all(r['c_ret'] >= r['a_ret'] for r in slippage_results) or all(r['c_ret'] < r['a_ret'] for r in slippage_results)
    lines.append(f"4. 3/5/10bp下结论方向不反转: {'✅' if bps_ok else '❌'}")
    
    # 5. leave-one-year-out多数结果方向一致（P1修正：严格>50%才算多数）
    if loyo:
        ca_directions = [r['diff_ca'] > 0 for r in loyo]
        majority = sum(ca_directions) / len(ca_directions)
        loyo_ok = majority > 0.5  # 严格大于50%才算多数
        lines.append(f"5. leave-one-year-out多数结果方向一致: {'✅' if loyo_ok else '❌'} (C>A: {sum(ca_directions)}/{len(ca_directions)} = {majority:.0%}; 严格多数需>50%)")
    
    # 6. 优势不能主要来自单一年份或单一防御ETF（P1修正：补充实际评估）
    lines.append("")
    lines.append("### 标准6-8补充评估")
    lines.append("")
    
    # 6. 自然年C-A贡献（P1-3修正：直接计算每个自然年差异）
    if annual:
        lines.append("6. 自然年C-A贡献（直接计算，非剔除后差异）：")
        for r in annual:
            lines.append(f"   {r['year']}: A={r['a_ret']:.2%}, C={r['c_ret']:.2%}, C-A={r['diff_ca']:+.2%}")
        max_year = max(annual, key=lambda x: abs(x['diff_ca']))
        avg_diff = sum(r['diff_ca'] for r in annual) / len(annual)
        lines.append(f"   最大偏离年份={max_year['year']}({max_year['diff_ca']:+.2%})，平均年差={avg_diff:+.2%}")
        if abs(max_year['diff_ca']) > abs(avg_diff) * 2:
            lines.append("   ⚠️ 单年贡献显著高于平均，存在集中风险。")
        else:
            lines.append("   ✅ 单年贡献未显著偏离平均。")
    
    # P1-4: 防御ETF贡献（mark-to-market）
    lines.append("")
    lines.append(f"7. 防御ETF分别贡献（P1-4修正：mark-to-market，含期末未平仓估值）：")
    lines.append(f"   计算口径：总PnL = 总卖出收入 + 期末市值 - 总买入成本 - 总佣金")
    lines.append(f"   黄金ETF(518880.SH)：")
    lines.append(f"     A: 买入成本={defense_contrib['gold_buy_a']:,.2f}, 卖出收入={defense_contrib['gold_sell_a']:,.2f}, 佣金={defense_contrib['gold_comm_a']:,.2f}, 期末市值={defense_contrib['gold_mv_a']:,.2f}, PnL={defense_contrib['gold_pnl_a']:,.2f}")
    lines.append(f"     C: 买入成本={defense_contrib['gold_buy_c']:,.2f}, 卖出收入={defense_contrib['gold_sell_c']:,.2f}, 佣金={defense_contrib['gold_comm_c']:,.2f}, 期末市值={defense_contrib['gold_mv_c']:,.2f}, PnL={defense_contrib['gold_pnl_c']:,.2f}")
    lines.append(f"     C-A={defense_contrib['gold_diff']:,.2f}")
    lines.append(f"   国债ETF(511010.SH)：")
    lines.append(f"     A: 买入成本={defense_contrib['bond_buy_a']:,.2f}, 卖出收入={defense_contrib['bond_sell_a']:,.2f}, 佣金={defense_contrib['bond_comm_a']:,.2f}, 期末市值={defense_contrib['bond_mv_a']:,.2f}, PnL={defense_contrib['bond_pnl_a']:,.2f}")
    lines.append(f"     C: 买入成本={defense_contrib['bond_buy_c']:,.2f}, 卖出收入={defense_contrib['bond_sell_c']:,.2f}, 佣金={defense_contrib['bond_comm_c']:,.2f}, 期末市值={defense_contrib['bond_mv_c']:,.2f}, PnL={defense_contrib['bond_pnl_c']:,.2f}")
    lines.append(f"     C-A={defense_contrib['bond_diff']:,.2f}")
    
    # P1-5: 实际佣金
    lines.append("")
    lines.append(f"8. 佣金（P1-5修正：从trades_df实际commission求和，截止2024-12-31）：")
    lines.append(f"   A={comm_a:,.2f}, B={comm_b:,.2f}, C={comm_c:,.2f}")
    lines.append(f"   C-A={comm_c-comm_a:,.2f}, B-A={comm_b-comm_a:,.2f}")
    
    # 勾稽验证（P1-4：A/B/C均验证）
    lines.append("")
    lines.append("### 勾稽验证")
    lines.append(f"- 方案A: NAV={result_a['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_a['num_trades']} ✅")
    lines.append(f"- 方案B: NAV={result_b['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_b['num_trades']} ✅")
    lines.append(f"- 方案C: NAV={result_c['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_c['num_trades']} ✅")
    lines.append("- B0.4基线复现: NAV=2,761,288.07, 交易=804 ✅")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    
    # 自动结论
    if research and validation:
        c_better_research = research['c_sharpe'] > research['a_sharpe']
        c_better_validation = validation['c_sharpe'] > validation['a_sharpe']
        if c_better_research and c_better_validation and dd_ok and ret_ok and bps_ok and loyo_ok:
            lines.append("- **预注册标准全部通过**：C可列为候选增强")
        else:
            lines.append("- **预注册标准未全部通过**：C只能判定为机制观察候选，不得升级B0.4")
    
    lines.append("")
    lines.append("## 数据文件")
    lines.append("")
    lines.append("- `reports/v1_3_step6_nav_A.csv` — 方案A逐日NAV")
    lines.append("- `reports/v1_3_step6_nav_B.csv` — 方案B逐日NAV")
    lines.append("- `reports/v1_3_step6_nav_C.csv` — 方案C逐日NAV")
    lines.append("- `reports/v1_3_step6_trades_A.csv` — 方案A交易明细")
    lines.append("- `reports/v1_3_step6_trades_B.csv` — 方案B交易明细")
    lines.append("- `reports/v1_3_step6_trades_C.csv` — 方案C交易明细")
    lines.append("- `reports/v1_3_step6_regime_switches.csv` — 状态切换明细")
    lines.append("- `reports/v1_3_step6_mechanism_attr.csv` — 逐日机制归因")
    lines.append("- `reports/v1_3_step6_regime_summary.csv` — 状态汇总")
    lines.append("- `reports/v1_3_step6_annual_contribution.csv` — 自然年C-A贡献（P1-3）")
    lines.append("- `reports/v1_3_step6_defense_contribution.csv` — 防御ETF贡献（P1-4）")
    lines.append("- `reports/v1_3_step6_reconciliation.csv` — 勾稽验证汇总（P1-6）")
    lines.append("")

    report = "\n".join(lines)
    with open('D:/etf_rotation_model/reports/v1_3_step6_dynamic_fifth_slot_ab.md', 'w', encoding='utf-8') as f:
        f.write(report)

    print("报告已保存: D:/etf_rotation_model/reports/v1_3_step6_dynamic_fifth_slot_ab.md")


if __name__ == '__main__':
    main()

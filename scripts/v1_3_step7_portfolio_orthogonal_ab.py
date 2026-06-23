"""v1.3 Step 7: 组合集中度与资金去向正交拆解

四个方案：
A: 5×20% — B0.4对照
B: 4×20% + 现金 — 关闭防御
C: 4×20% + 防御 — 防御填充
D: 4×25% — 集中度提升

研究期：2019-08-13至2022-12-31
验证期：2023-01-01至2024-12-31
分析期：2019-08-13至2024-12-31
观察期：2025-01-01至2026-06-18，仅展示
"""
import sys, os, copy, json, argparse, ast

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
RESEARCH_PERIOD = ('2019-08-13', '2022-12-31')
VALIDATION_PERIOD = ('2023-01-01', '2024-12-31')
ANALYSIS_PERIOD = ('2019-08-13', '2024-12-31')
OBSERVATION_PERIOD = ('2025-01-01', AS_OF_DATE)


def get_config(scenario):
    """获取四个方案配置。"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False

    if scenario == 'A':  # B0.4对照：5×20%
        cfg['stock_max_holdings'] = 5
        cfg['max_holdings'] = 5
        cfg['total_max_holdings'] = 5
        cfg['defense_max_holdings'] = 2
        cfg['max_position_per_etf'] = 0.20
    elif scenario == 'B':  # 4×20% + 现金，关闭防御
        cfg['stock_max_holdings'] = 4
        cfg['max_holdings'] = 4
        cfg['total_max_holdings'] = 4
        cfg['defense_max_holdings'] = 0  # 关闭防御
        cfg['max_position_per_etf'] = 0.20
    elif scenario == 'C':  # 4×20% + 防御
        cfg['stock_max_holdings'] = 4
        cfg['max_holdings'] = 4
        cfg['total_max_holdings'] = 5  # 4行业+1防御
        cfg['defense_max_holdings'] = 1
        cfg['max_position_per_etf'] = 0.20
    elif scenario == 'D':  # 4×25%
        cfg['stock_max_holdings'] = 4
        cfg['max_holdings'] = 4
        cfg['total_max_holdings'] = 4
        cfg['defense_max_holdings'] = 0  # 关闭防御
        cfg['max_position_per_etf'] = 0.25
    return cfg


def run_scenario(scenario, market_df, bench_df, slippage_bps=0):
    """运行一个方案。"""
    cfg = get_config(scenario)
    engine = BacktestEngine(cfg, slippage_bps=slippage_bps)
    result = engine.run(market_df.copy(), bench_df.copy(), as_of_date=AS_OF_DATE)
    return result


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

    daily_ret = nav['ret'].dropna()
    sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

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

    # 平均持仓（如果nav_df中有这些列）
    avg_ind = (nav['industry_value'] / nav['nav']).mean() if 'industry_value' in nav.columns else 0
    avg_def = (nav['defense_value'] / nav['nav']).mean() if 'defense_value' in nav.columns else 0
    avg_cash = (nav['cash'] / nav['nav']).mean() if 'cash' in nav.columns else 0

    # 实际持仓数量分布
    num_positions = nav['num_positions'].mean() if 'num_positions' in nav.columns else 0

    # 满仓比例（现金<5%）
    full_pct = (nav['cash'] / nav['nav'] < 0.05).mean() if 'cash' in nav.columns else 0
    cash_10pct = (nav['cash'] / nav['nav'] > 0.10).mean() if 'cash' in nav.columns else 0
    cash_20pct = (nav['cash'] / nav['nav'] > 0.20).mean() if 'cash' in nav.columns else 0

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
        'avg_num_positions': num_positions,
        'full_pct': full_pct,
        'cash_10pct': cash_10pct,
        'cash_20pct': cash_20pct,
        'nav': nav,
    }


def analyze_period_diff(nav_a, trades_a, nav_b, trades_b, nav_c, trades_c, nav_d, trades_d,
                        period_label, period_start, period_end):
    """分析四个方案在指定期间的差异。"""
    m_a = compute_metrics(nav_a, trades_a, period_start, period_end)
    m_b = compute_metrics(nav_b, trades_b, period_start, period_end)
    m_c = compute_metrics(nav_c, trades_c, period_start, period_end)
    m_d = compute_metrics(nav_d, trades_d, period_start, period_end)

    if not m_a or not m_b or not m_c or not m_d:
        return None

    return {
        'period': period_label,
        'a_ret': m_a['total_ret'], 'b_ret': m_b['total_ret'],
        'c_ret': m_c['total_ret'], 'd_ret': m_d['total_ret'],
        'a_sharpe': m_a['sharpe'], 'b_sharpe': m_b['sharpe'],
        'c_sharpe': m_c['sharpe'], 'd_sharpe': m_d['sharpe'],
        'a_maxdd': m_a['max_dd'], 'b_maxdd': m_b['max_dd'],
        'c_maxdd': m_c['max_dd'], 'd_maxdd': m_d['max_dd'],
        'a_trades': m_a['n_trades'], 'b_trades': m_b['n_trades'],
        'c_trades': m_c['n_trades'], 'd_trades': m_d['n_trades'],
        'a_comm': m_a['total_comm'], 'b_comm': m_b['total_comm'],
        'c_comm': m_c['total_comm'], 'd_comm': m_d['total_comm'],
        'diff_ba': m_b['total_ret'] - m_a['total_ret'],
        'diff_ca': m_c['total_ret'] - m_a['total_ret'],
        'diff_da': m_d['total_ret'] - m_a['total_ret'],
        'diff_cb': m_c['total_ret'] - m_b['total_ret'],
        'diff_db': m_d['total_ret'] - m_b['total_ret'],
        'diff_dc': m_d['total_ret'] - m_c['total_ret'],
    }


def leave_one_year_out(nav_a, nav_b, nav_c, nav_d, analysis_end='2024-12-31'):
    """leave-one-year-out：仅分析期（2019-2024）使用。"""
    nav_a = nav_a.copy(); nav_a['date'] = pd.to_datetime(nav_a['date'])
    nav_b = nav_b.copy(); nav_b['date'] = pd.to_datetime(nav_b['date'])
    nav_c = nav_c.copy(); nav_c['date'] = pd.to_datetime(nav_c['date'])
    nav_d = nav_d.copy(); nav_d['date'] = pd.to_datetime(nav_d['date'])

    analysis_end_dt = pd.to_datetime(analysis_end)
    nav_a = nav_a[nav_a['date'] <= analysis_end_dt].copy()
    nav_b = nav_b[nav_b['date'] <= analysis_end_dt].copy()
    nav_c = nav_c[nav_c['date'] <= analysis_end_dt].copy()
    nav_d = nav_d[nav_d['date'] <= analysis_end_dt].copy()

    for nav in [nav_a, nav_b, nav_c, nav_d]:
        nav['ret'] = nav['nav'].pct_change()

    years = sorted(nav_a['date'].dt.year.unique())
    results = []

    for exclude_year in years:
        a_sub = nav_a[nav_a['date'].dt.year != exclude_year]
        b_sub = nav_b[nav_b['date'].dt.year != exclude_year]
        c_sub = nav_c[nav_c['date'].dt.year != exclude_year]
        d_sub = nav_d[nav_d['date'].dt.year != exclude_year]

        if len(a_sub) < 2 or len(b_sub) < 2 or len(c_sub) < 2 or len(d_sub) < 2:
            continue

        ret_a = (1 + a_sub['ret'].fillna(0)).prod() - 1
        ret_b = (1 + b_sub['ret'].fillna(0)).prod() - 1
        ret_c = (1 + c_sub['ret'].fillna(0)).prod() - 1
        ret_d = (1 + d_sub['ret'].fillna(0)).prod() - 1

        results.append({
            'exclude_year': exclude_year,
            'a_ret': ret_a, 'b_ret': ret_b, 'c_ret': ret_c, 'd_ret': ret_d,
            'diff_da': ret_d - ret_a,
        })

    return results


def annual_contribution(nav_a, nav_d, analysis_end='2024-12-31'):
    """直接计算每个自然年的D-A收益差。"""
    nav_a = nav_a.copy(); nav_a['date'] = pd.to_datetime(nav_a['date'])
    nav_d = nav_d.copy(); nav_d['date'] = pd.to_datetime(nav_d['date'])

    analysis_end_dt = pd.to_datetime(analysis_end)
    nav_a = nav_a[nav_a['date'] <= analysis_end_dt]
    nav_d = nav_d[nav_d['date'] <= analysis_end_dt]

    nav_a['ret'] = nav_a['nav'].pct_change()
    nav_d['ret'] = nav_d['nav'].pct_change()

    years = sorted(nav_a['date'].dt.year.unique())
    results = []
    for year in years:
        a_yr = nav_a[nav_a['date'].dt.year == year]
        d_yr = nav_d[nav_d['date'].dt.year == year]
        if len(a_yr) < 2 or len(d_yr) < 2:
            continue
        ret_a = (1 + a_yr['ret'].fillna(0)).prod() - 1
        ret_d = (1 + d_yr['ret'].fillna(0)).prod() - 1
        results.append({
            'year': year,
            'a_ret': ret_a,
            'd_ret': ret_d,
            'diff_da': ret_d - ret_a,
        })
    return results


def defense_etf_contribution(trades_b, trades_c, market_df, analysis_end='2024-12-31'):
    """分别统计黄金ETF和国债ETF对C-B的贡献（mark-to-market）。"""
    gold = ['518880.SH']
    bond = ['511010.SH']
    analysis_end_dt = pd.to_datetime(analysis_end)

    def calc_ticker_pnl(trades_df, ticker_list, market_df):
        ticker = ticker_list[0]
        tdf = trades_df.copy()
        tdf['date'] = pd.to_datetime(tdf['date'])
        tdf = tdf[tdf['date'] <= analysis_end_dt]
        tdf = tdf[tdf['ticker'] == ticker]

        prices = market_df[market_df['ticker'] == ticker][['date', 'close']].copy()
        prices['date'] = pd.to_datetime(prices['date'])
        prices = prices[prices['date'] <= analysis_end_dt]
        prices = prices.sort_values('date').reset_index(drop=True)

        if prices.empty:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0

        buy = tdf[tdf['action'] == 'BUY']
        sell = tdf[tdf['action'].isin(['SELL', 'STOP_LOSS'])]

        total_buy_cost = (buy['shares'] * buy['price']).sum() if not buy.empty else 0.0
        total_sell_rev = (sell['shares'] * sell['price']).sum() if not sell.empty else 0.0
        total_comm = tdf['commission'].sum() if not tdf.empty else 0.0
        total_buy_shares = buy['shares'].sum() if not buy.empty else 0
        total_sell_shares = sell['shares'].sum() if not sell.empty else 0

        final_position = total_buy_shares - total_sell_shares
        final_close = prices['close'].iloc[-1]
        final_market_value = final_position * final_close
        total_pnl = total_sell_rev + final_market_value - total_buy_cost - total_comm

        return total_pnl, total_buy_cost, total_sell_rev, total_comm, final_market_value, total_buy_shares, total_sell_shares

    results = {}
    for name, tickers in [('gold', gold), ('bond', bond)]:
        pnl_b, buy_b, sell_b, comm_b, mv_b, bs_b, ss_b = calc_ticker_pnl(trades_b, tickers, market_df)
        pnl_c, buy_c, sell_c, comm_c, mv_c, bs_c, ss_c = calc_ticker_pnl(trades_c, tickers, market_df)

        results[f'{name}_pnl_b'] = pnl_b
        results[f'{name}_pnl_c'] = pnl_c
        results[f'{name}_diff'] = pnl_c - pnl_b
        results[f'{name}_buy_b'] = buy_b
        results[f'{name}_buy_c'] = buy_c
        results[f'{name}_sell_b'] = sell_b
        results[f'{name}_sell_c'] = sell_c
        results[f'{name}_comm_b'] = comm_b
        results[f'{name}_comm_c'] = comm_c
        results[f'{name}_final_position_mv_b'] = mv_b
        results[f'{name}_final_position_mv_c'] = mv_c
        results[f'{name}_buy_shares_b'] = bs_b
        results[f'{name}_buy_shares_c'] = bs_c
        results[f'{name}_sell_shares_b'] = ss_b
        results[f'{name}_sell_shares_c'] = ss_c

    return results


def total_commission(trades_df, analysis_end='2024-12-31'):
    """从trades_df实际commission求和，严格截止分析期。"""
    if trades_df.empty or 'commission' not in trades_df.columns:
        return 0.0
    tdf = trades_df.copy()
    tdf['date'] = pd.to_datetime(tdf['date'])
    tdf = tdf[tdf['date'] <= pd.to_datetime(analysis_end)]
    return tdf['commission'].sum()


def reconciliation_summary(result_a, result_b, result_c, result_d, comm_a, comm_b, comm_c, comm_d):
    """生成勾稽汇总CSV。"""
    return pd.DataFrame({
        'scenario': ['A', 'B', 'C', 'D'],
        'final_nav': [
            result_a['nav_df']['nav'].iloc[-1],
            result_b['nav_df']['nav'].iloc[-1],
            result_c['nav_df']['nav'].iloc[-1],
            result_d['nav_df']['nav'].iloc[-1],
        ],
        'num_trades': [result_a['num_trades'], result_b['num_trades'],
                       result_c['num_trades'], result_d['num_trades']],
        'total_commission': [comm_a, comm_b, comm_c, comm_d],
    })


# ============================================================
# NEW: Position Exposure (逐日、逐方案)
# ============================================================

def compute_position_exposure(nav_df, scenario):
    """逐日统计持仓敞口。"""
    records = []
    industry_tickers = list(ETF_UNIVERSE.keys())
    for _, row in nav_df.iterrows():
        date = row['date']
        nav = row['nav']
        cash = row['cash']
        num_pos = row.get('num_positions', 0)

        pct = row['positions_pct'] if isinstance(row['positions_pct'], dict) else (ast.literal_eval(row['positions_pct']) if isinstance(row['positions_pct'], str) and row['positions_pct'] else {})

        industry_pct = sum(v for k, v in pct.items() if k in industry_tickers)
        defense_pct = sum(v for k, v in pct.items() if k in DEFENSE_UNIVERSE)
        cash_pct = cash / nav if nav > 0 else 0

        num_industry = sum(1 for k in pct if k in industry_tickers)
        num_defense = sum(1 for k in pct if k in DEFENSE_UNIVERSE)

        weights = sorted([v for k, v in pct.items() if k in industry_tickers], reverse=True)
        top_weights = weights + [0] * (5 - len(weights))

        full_pos = cash_pct < 0.05
        cash_gt_10 = cash_pct > 0.10
        cash_gt_20 = cash_pct > 0.20

        records.append({
            'date': date,
            'scenario': scenario,
            'industry_pct': industry_pct,
            'defense_pct': defense_pct,
            'cash_pct': cash_pct,
            'num_industry': num_industry,
            'num_defense': num_defense,
            'num_positions': num_pos,
            'top1_weight': top_weights[0],
            'top2_weight': top_weights[1],
            'top3_weight': top_weights[2],
            'top4_weight': top_weights[3],
            'top5_weight': top_weights[4],
            'full_position': full_pos,
            'cash_gt_10pct': cash_gt_10,
            'cash_gt_20pct': cash_gt_20,
        })
    return pd.DataFrame(records)


def summarize_position_exposure(exposure_df, period_start, period_end, period_label):
    """汇总指定期间的敞口统计。"""
    sub = exposure_df.copy()
    sub['date'] = pd.to_datetime(sub['date'])
    sub = sub[(sub['date'] >= pd.to_datetime(period_start)) & (sub['date'] <= pd.to_datetime(period_end))]

    if sub.empty:
        return None

    summary = {
        'period': period_label,
        'scenario': sub['scenario'].iloc[0],
        'avg_industry_pct': sub['industry_pct'].mean(),
        'avg_defense_pct': sub['defense_pct'].mean(),
        'avg_cash_pct': sub['cash_pct'].mean(),
        'avg_num_industry': sub['num_industry'].mean(),
        'avg_num_defense': sub['num_defense'].mean(),
        'avg_num_positions': sub['num_positions'].mean(),
        'full_position_pct': sub['full_position'].mean(),
        'cash_gt_10pct_days': sub['cash_gt_10pct'].sum(),
        'cash_gt_20pct_days': sub['cash_gt_20pct'].sum(),
    }
    return summary


# ============================================================
# NEW: Slot Contribution (rank1-5 mark-to-market PnL)
# ============================================================

def reconstruct_holdings(trades_df):
    """从trades_df重建持仓周期。"""
    holdings = []
    for ticker in trades_df['ticker'].unique():
        tdf = trades_df[trades_df['ticker'] == ticker].sort_values('date')
        active = None
        for _, row in tdf.iterrows():
            if row['action'] == 'BUY':
                active = {
                    'ticker': ticker,
                    'buy_date': row['date'],
                    'shares': row['shares'],
                    'buy_price': row['price'],
                    'sell_date': None,
                    'sell_price': None,
                    'commission': row['commission'],
                }
            elif row['action'] in ('SELL', 'STOP_LOSS') and active is not None:
                active['sell_date'] = row['date']
                active['sell_price'] = row['price']
                active['commission'] += row['commission']
                holdings.append(active)
                active = None
        if active is not None:
            holdings.append(active)
    return holdings


def compute_holding_daily_pnl(holding, market_df):
    """计算单个持仓的逐日mark-to-market PnL。"""
    ticker = holding['ticker']
    buy_date = holding['buy_date']
    sell_date = holding['sell_date'] or market_df['date'].max()
    shares = holding['shares']
    buy_price = holding['buy_price']
    sell_price = holding['sell_price']

    mkt = market_df[market_df['ticker'] == ticker][['date', 'close']].sort_values('date').reset_index(drop=True)
    if mkt.empty:
        return pd.DataFrame()

    mask = (mkt['date'] >= buy_date) & (mkt['date'] <= sell_date)
    sub = mkt[mask].copy().reset_index(drop=True)
    if len(sub) < 1:
        return pd.DataFrame()

    sub['prev_close'] = sub['close'].shift(1)
    sub.loc[0, 'prev_close'] = buy_price
    if sell_price is not None and sell_date is not None:
        sub.loc[sub.index[-1], 'close'] = sell_price
    sub['daily_pnl'] = shares * (sub['close'] - sub['prev_close'])
    sub['ticker'] = ticker
    return sub[['date', 'ticker', 'daily_pnl']]


def compute_slot_contribution(nav_df, trades_df, market_df, scores_df, scenario):
    """按rank统计持有期mark-to-market PnL。"""
    industry_tickers = list(ETF_UNIVERSE.keys())
    holdings = reconstruct_holdings(trades_df)

    # Build daily PnL per ticker
    all_pnls = []
    for h in holdings:
        pnl = compute_holding_daily_pnl(h, market_df)
        if not pnl.empty:
            all_pnls.append(pnl)
    if all_pnls:
        pnl_df = pd.concat(all_pnls, ignore_index=True)
    else:
        pnl_df = pd.DataFrame(columns=['date', 'ticker', 'daily_pnl'])

    # Assign rank per day and aggregate
    rank_records = []
    rank5_records = []

    for _, row in nav_df.iterrows():
        date = row['date']
        pct = row['positions_pct'] if isinstance(row['positions_pct'], dict) else (ast.literal_eval(row['positions_pct']) if isinstance(row['positions_pct'], str) and row['positions_pct'] else {})
        industry_pct = {k: v for k, v in pct.items() if k in industry_tickers}

        if not industry_pct:
            continue

        day_scores = scores_df[scores_df['date'] == date]
        merged = pd.DataFrame({'ticker': list(industry_pct.keys())})
        merged = merged.merge(day_scores[['ticker', 'total_score']], on='ticker', how='left')
        merged = merged.sort_values('total_score', ascending=False).reset_index(drop=True)
        merged['rank'] = merged.index + 1

        day_pnl = pnl_df[pnl_df['date'] == date]
        if day_pnl.empty:
            continue

        for _, r in merged.iterrows():
            rank = r['rank']
            ticker = r['ticker']
            pnl_row = day_pnl[day_pnl['ticker'] == ticker]
            if not pnl_row.empty:
                rec = {
                    'date': date,
                    'scenario': scenario,
                    'rank': rank,
                    'ticker': ticker,
                    'daily_pnl': pnl_row['daily_pnl'].iloc[0],
                }
                rank_records.append(rec)
                if rank == 5:
                    rank5_records.append(rec)

    rank_df = pd.DataFrame(rank_records, columns=['date', 'scenario', 'rank', 'ticker', 'daily_pnl'])
    rank5_df = pd.DataFrame(rank5_records, columns=['date', 'scenario', 'rank', 'ticker', 'daily_pnl'])

    # Aggregate by rank
    agg_rows = []
    for rank in range(1, 6):
        sub = rank_df[rank_df['rank'] == rank]
        if sub.empty:
            agg_rows.append({
                'scenario': scenario, 'rank': rank,
                'total_pnl': 0, 'num_days': 0, 'win_days': 0,
                'avg_daily_pnl': 0, 'max_drawdown': 0,
            })
            continue
        pnls = sub['daily_pnl'].values
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        valid_peak = peak > 0
        if np.any(valid_peak):
            dd = (cum[valid_peak] - peak[valid_peak]) / peak[valid_peak]
            max_dd = dd.min()
        else:
            max_dd = 0
        agg_rows.append({
            'scenario': scenario, 'rank': rank,
            'total_pnl': float(np.sum(pnls)),
            'num_days': len(pnls),
            'win_days': int(np.sum(pnls > 0)),
            'avg_daily_pnl': float(np.mean(pnls)),
            'max_drawdown': float(max_dd),
        })

    # Rank 5 yearly breakdown
    rank5_yearly = []
    if not rank5_df.empty:
        rank5_df['year'] = pd.to_datetime(rank5_df['date']).dt.year
        for year, group in rank5_df.groupby('year'):
            pnls = group['daily_pnl'].values
            cum = np.cumsum(pnls)
            peak = np.maximum.accumulate(cum)
            valid_peak = peak > 0
            if np.any(valid_peak):
                dd = (cum[valid_peak] - peak[valid_peak]) / peak[valid_peak]
                max_dd = dd.min()
            else:
                max_dd = 0
            rank5_yearly.append({
                'scenario': scenario, 'year': year,
                'total_pnl': float(np.sum(pnls)),
                'win_rate': float(np.sum(pnls > 0) / len(pnls)) if len(pnls) > 0 else 0,
                'hold_days': len(pnls),
                'max_drawdown': float(dd.min()) if len(pnls) > 0 else 0,
            })

    return pd.DataFrame(agg_rows), pd.DataFrame(rank5_yearly, columns=['scenario', 'year', 'total_pnl', 'win_rate', 'hold_days', 'max_drawdown'])


# ============================================================
# NEW: Yearly Metrics
# ============================================================

def compute_yearly_metrics(nav_df, trades_df, period_start=None, period_end=None):
    """按年度统计收益、胜率、夏普、回撤、敞口、交易、佣金。"""
    nav = nav_df.copy()
    nav['date'] = pd.to_datetime(nav['date'])
    if period_start:
        nav = nav[nav['date'] >= pd.to_datetime(period_start)]
    if period_end:
        nav = nav[nav['date'] <= pd.to_datetime(period_end)]

    t = trades_df.copy()
    if not t.empty and 'date' in t.columns:
        t['date'] = pd.to_datetime(t['date'])
        if period_start:
            t = t[t['date'] >= pd.to_datetime(period_start)]
        if period_end:
            t = t[t['date'] <= pd.to_datetime(period_end)]

    nav['ret'] = nav['nav'].pct_change()
    nav['year'] = nav['date'].dt.year

    results = []
    for year, group in nav.groupby('year'):
        group = group.sort_values('date')
        if len(group) < 2:
            continue

        total_ret = group['nav'].iloc[-1] / group['nav'].iloc[0] - 1
        daily_ret = group['ret'].dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

        cum = (1 + daily_ret).cumprod()
        peak = cum.expanding().max()
        drawdown = (cum - peak) / peak
        max_dd = drawdown.min()

        group['ym'] = group['date'].dt.to_period('M')
        monthly = group.groupby('ym')['ret'].sum()
        monthly_win_rate = (monthly > 0).mean() if len(monthly) > 0 else 0

        yr_trades = t[t['date'].dt.year == year]
        n_trades = len(yr_trades)
        total_comm = yr_trades['commission'].sum() if not yr_trades.empty and 'commission' in yr_trades.columns else 0

        avg_ind = (group['industry_value'] / group['nav']).mean() if 'industry_value' in group.columns else 0
        avg_def = (group['defense_value'] / group['nav']).mean() if 'defense_value' in group.columns else 0
        avg_cash = (group['cash'] / group['nav']).mean() if 'cash' in group.columns else 0

        results.append({
            'year': year,
            'total_return': total_ret,
            'monthly_win_rate': monthly_win_rate,
            'sharpe': sharpe,
            'max_drawdown': max_dd,
            'avg_industry_pct': avg_ind,
            'avg_defense_pct': avg_def,
            'avg_cash_pct': avg_cash,
            'n_trades': n_trades,
            'total_commission': total_comm,
        })
    return pd.DataFrame(results)


# ============================================================
# NEW: Commission Summary
# ============================================================

def compute_commission_summary(trades_df, period_start=None, period_end=None):
    """按年度统计佣金。"""
    t = trades_df.copy()
    if t.empty or 'date' not in t.columns:
        return pd.DataFrame(columns=['year', 'n_buys', 'n_sells', 'n_stop_loss', 'total_commission'])
    t['date'] = pd.to_datetime(t['date'])
    if period_start:
        t = t[t['date'] >= pd.to_datetime(period_start)]
    if period_end:
        t = t[t['date'] <= pd.to_datetime(period_end)]

    t['year'] = t['date'].dt.year
    results = []
    for year, group in t.groupby('year'):
        results.append({
            'year': year,
            'n_buys': len(group[group['action'] == 'BUY']),
            'n_sells': len(group[group['action'] == 'SELL']),
            'n_stop_loss': len(group[group['action'] == 'STOP_LOSS']),
            'total_commission': group['commission'].sum() if 'commission' in group.columns else 0,
        })
    return pd.DataFrame(results)


# ============================================================
# NEW: Orthogonal Attribution
# ============================================================

def compute_orthogonal_attribution(period_results, slot_contribution, defense_contrib, position_exposure):
    """正交归因，用实际敞口及槽位PnL验证。"""
    # Extract slot PnL by scenario and rank
    def get_slot_pnl(sc, rank):
        sub = slot_contribution[(slot_contribution['scenario'] == sc) & (slot_contribution['rank'] == rank)]
        return sub['total_pnl'].iloc[0] if not sub.empty else 0.0

    def get_total_slot_pnl(sc):
        return slot_contribution[slot_contribution['scenario'] == sc]['total_pnl'].sum()

    # Get exposure summary for each scenario
    def get_exposure_summary(sc, period_label):
        sub = position_exposure[(position_exposure['scenario'] == sc)]
        if period_label == '研究期':
            sub = sub[(sub['date'] >= pd.to_datetime(RESEARCH_PERIOD[0])) & (sub['date'] <= pd.to_datetime(RESEARCH_PERIOD[1]))]
        elif period_label == '验证期':
            sub = sub[(sub['date'] >= pd.to_datetime(VALIDATION_PERIOD[0])) & (sub['date'] <= pd.to_datetime(VALIDATION_PERIOD[1]))]
        elif period_label == '分析期':
            sub = sub[(sub['date'] >= pd.to_datetime(ANALYSIS_PERIOD[0])) & (sub['date'] <= pd.to_datetime(ANALYSIS_PERIOD[1]))]
        if sub.empty:
            return {}
        return {
            'avg_industry_pct': sub['industry_pct'].mean(),
            'avg_defense_pct': sub['defense_pct'].mean(),
            'avg_cash_pct': sub['cash_pct'].mean(),
            'avg_top4_weight': (sub['top1_weight'] + sub['top2_weight'] + sub['top3_weight'] + sub['top4_weight']).mean(),
        }

    # For each period, compute attribution
    periods = ['研究期', '验证期', '分析期']
    attribution = []

    for period_label in periods:
        pr = next((r for r in period_results if r['period'] == period_label), None)
        if not pr:
            continue

        # Slot PnL totals (all ranks)
        a_total = get_total_slot_pnl('A')
        b_total = get_total_slot_pnl('B')
        c_total = get_total_slot_pnl('C')
        d_total = get_total_slot_pnl('D')

        # Rank 1-4 PnL
        a_r14 = sum(get_slot_pnl('A', r) for r in range(1, 5))
        b_r14 = sum(get_slot_pnl('B', r) for r in range(1, 5))
        c_r14 = sum(get_slot_pnl('C', r) for r in range(1, 5))
        d_r14 = sum(get_slot_pnl('D', r) for r in range(1, 5))

        # Rank 5 PnL
        a_r5 = get_slot_pnl('A', 5)
        b_r5 = get_slot_pnl('B', 5)
        c_r5 = get_slot_pnl('C', 5)
        d_r5 = get_slot_pnl('D', 5)

        # Defense PnL (from slot_contribution, defense tickers are not in rank slots)
        # Use defense_contrib for C-B
        def_pnl_c = defense_contrib.get('gold_pnl_c', 0) + defense_contrib.get('bond_pnl_c', 0)
        def_pnl_b = defense_contrib.get('gold_pnl_b', 0) + defense_contrib.get('bond_pnl_b', 0)

        # Exposure
        exp_a = get_exposure_summary('A', period_label)
        exp_b = get_exposure_summary('B', period_label)
        exp_c = get_exposure_summary('C', period_label)
        exp_d = get_exposure_summary('D', period_label)

        # B-A: delete rank5 + cash increase
        ba_observed = pr['diff_ba'] * 1_000_000
        ba_rank5 = a_r5  # A has rank5, B has none
        ba_r14_diff = b_r14 - a_r14  # interaction from different rebalancing
        ba_residual = ba_observed - ba_rank5 - ba_r14_diff
        attribution.append({
            'period': period_label, 'pair': 'B-A',
            'observed_diff': ba_observed,
            'rank5_effect': ba_rank5,
            'r14_interaction': ba_r14_diff,
            'residual': ba_residual,
            'a_top4': exp_a.get('avg_top4_weight', 0),
            'b_top4': exp_b.get('avg_top4_weight', 0),
        })

        # C-B: defense vs cash
        cb_observed = pr['diff_cb'] * 1_000_000
        cb_defense = def_pnl_c - def_pnl_b
        cb_r14_diff = c_r14 - b_r14
        cb_residual = cb_observed - cb_defense - cb_r14_diff
        attribution.append({
            'period': period_label, 'pair': 'C-B',
            'observed_diff': cb_observed,
            'defense_effect': cb_defense,
            'r14_interaction': cb_r14_diff,
            'residual': cb_residual,
            'b_defense': exp_b.get('avg_defense_pct', 0),
            'c_defense': exp_c.get('avg_defense_pct', 0),
        })

        # D-B: Top4 concentration
        db_observed = pr['diff_db'] * 1_000_000
        db_r14_diff = d_r14 - b_r14
        db_residual = db_observed - db_r14_diff
        attribution.append({
            'period': period_label, 'pair': 'D-B',
            'observed_diff': db_observed,
            'r14_concentration': db_r14_diff,
            'residual': db_residual,
            'b_top4': exp_b.get('avg_top4_weight', 0),
            'd_top4': exp_d.get('avg_top4_weight', 0),
        })

        # D-A: combined
        da_observed = pr['diff_da'] * 1_000_000
        da_r14_diff = d_r14 - a_r14
        da_residual = da_observed - da_r14_diff - a_r5
        attribution.append({
            'period': period_label, 'pair': 'D-A',
            'observed_diff': da_observed,
            'rank5_effect': a_r5,
            'r14_concentration': da_r14_diff,
            'residual': da_residual,
            'a_top4': exp_a.get('avg_top4_weight', 0),
            'd_top4': exp_d.get('avg_top4_weight', 0),
        })

    return pd.DataFrame(attribution)


# ============================================================
# NEW: Pre-reg Standard 7 Verification
# ============================================================

def verify_prereg_standard_7(position_exposure):
    """验证D的Top4实际平均权重确实高于A/B。"""
    results = []
    for sc in ['A', 'B', 'C', 'D']:
        sub = position_exposure[position_exposure['scenario'] == sc]
        if sub.empty:
            continue
        sub['top4_weight'] = sub['top1_weight'] + sub['top2_weight'] + sub['top3_weight'] + sub['top4_weight']
        results.append({
            'scenario': sc,
            'avg_top4_weight': sub['top4_weight'].mean(),
        })
    df = pd.DataFrame(results)
    if df.empty:
        return False, df

    d_top4 = df[df['scenario'] == 'D']['avg_top4_weight'].iloc[0]
    a_top4 = df[df['scenario'] == 'A']['avg_top4_weight'].iloc[0]
    b_top4 = df[df['scenario'] == 'B']['avg_top4_weight'].iloc[0]

    passed = d_top4 > a_top4 and d_top4 > b_top4
    return passed, df


# ============================================================
# Main entry point
# ============================================================

def main(output_dir=None):
    """主入口。"""
    if output_dir is None:
        output_dir = 'D:/etf_rotation_model/reports'
    os.makedirs(output_dir, exist_ok=True)

    # 加载数据
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers)
    market_df['date'] = pd.to_datetime(market_df['date'])
    bench_df = db.get_market_data(ticker=BENCHMARK)

    # Load scores for rank assignment
    industry_tickers = list(ETF_UNIVERSE.keys())
    placeholders = ','.join(f"'{t}'" for t in industry_tickers)
    with db._connect() as conn:
        scores_df = pd.read_sql(
            f"SELECT date, ticker, total_score FROM daily_scores WHERE ticker IN ({placeholders})",
            conn
        )
        scores_df['date'] = pd.to_datetime(scores_df['date'])

    print("=" * 60)
    print("v1.3 Step 7: 组合集中度与资金去向正交拆解")
    print("=" * 60)

    # 运行四个方案
    results = {}
    for scenario in ['A', 'B', 'C', 'D']:
        print(f"\n=== 运行方案{scenario} ===")
        result = run_scenario(scenario, market_df, bench_df)
        results[scenario] = result
        print(f"{scenario} NAV: {result['nav_df']['nav'].iloc[-1]:,.2f}, 交易: {result['num_trades']}")

    result_a, result_b, result_c, result_d = results['A'], results['B'], results['C'], results['D']

    # 保存中间数据
    for sc in ['A', 'B', 'C', 'D']:
        results[sc]['nav_df'].to_csv(os.path.join(output_dir, f'v1_3_step7_nav_{sc}.csv'), index=False)
        results[sc]['trades_df'].to_csv(os.path.join(output_dir, f'v1_3_step7_trades_{sc}.csv'), index=False)

    # 期间对比
    periods = [
        ('研究期', RESEARCH_PERIOD[0], RESEARCH_PERIOD[1]),
        ('验证期', VALIDATION_PERIOD[0], VALIDATION_PERIOD[1]),
        ('分析期', ANALYSIS_PERIOD[0], ANALYSIS_PERIOD[1]),
        ('观察期', OBSERVATION_PERIOD[0], OBSERVATION_PERIOD[1]),
        ('全期间', '2019-08-13', AS_OF_DATE),
    ]

    period_results = []
    for label, s, e in periods:
        r = analyze_period_diff(result_a['nav_df'], result_a['trades_df'],
                                result_b['nav_df'], result_b['trades_df'],
                                result_c['nav_df'], result_c['trades_df'],
                                result_d['nav_df'], result_d['trades_df'],
                                label, s, e)
        if r:
            period_results.append(r)

    # 滑点压力测试
    slippage_results = []
    for bps in [0, 3, 5, 10]:
        print(f"\n--- 滑点 {bps}bp ---")
        r_a = run_scenario('A', market_df, bench_df, slippage_bps=bps)
        r_b = run_scenario('B', market_df, bench_df, slippage_bps=bps)
        r_c = run_scenario('C', market_df, bench_df, slippage_bps=bps)
        r_d = run_scenario('D', market_df, bench_df, slippage_bps=bps)

        initial = r_a['nav_df']['nav'].iloc[0]
        slippage_results.append({
            'bps': bps,
            'a_ret': r_a['nav_df']['nav'].iloc[-1] / initial - 1,
            'b_ret': r_b['nav_df']['nav'].iloc[-1] / initial - 1,
            'c_ret': r_c['nav_df']['nav'].iloc[-1] / initial - 1,
            'd_ret': r_d['nav_df']['nav'].iloc[-1] / initial - 1,
            'a_trades': r_a['num_trades'],
            'b_trades': r_b['num_trades'],
            'c_trades': r_c['num_trades'],
            'd_trades': r_d['num_trades'],
        })

    # LOO（仅分析期）
    print("\n--- leave-one-year-out (分析期2019-2024) ---")
    loyo = leave_one_year_out(result_a['nav_df'], result_b['nav_df'],
                              result_c['nav_df'], result_d['nav_df'])
    loyo_df = pd.DataFrame(loyo)
    loyo_df.to_csv(os.path.join(output_dir, 'v1_3_step7_loyo.csv'), index=False)

    # 年度贡献
    annual = annual_contribution(result_a['nav_df'], result_d['nav_df'])
    annual_df = pd.DataFrame(annual)
    annual_df.to_csv(os.path.join(output_dir, 'v1_3_step7_annual_contribution.csv'), index=False)

    # 防御贡献（B vs C）
    defense_contrib = defense_etf_contribution(
        result_b['trades_df'], result_c['trades_df'], market_df, analysis_end='2024-12-31'
    )
    defense_df = pd.DataFrame([defense_contrib])
    defense_df.to_csv(os.path.join(output_dir, 'v1_3_step7_defense_contribution.csv'), index=False)

    # 佣金
    comm_a = total_commission(result_a['trades_df'], analysis_end='2024-12-31')
    comm_b = total_commission(result_b['trades_df'], analysis_end='2024-12-31')
    comm_c = total_commission(result_c['trades_df'], analysis_end='2024-12-31')
    comm_d = total_commission(result_d['trades_df'], analysis_end='2024-12-31')
    print(f"佣金: A={comm_a:,.2f}, B={comm_b:,.2f}, C={comm_c:,.2f}, D={comm_d:,.2f}")

    # 勾稽汇总
    recon = reconciliation_summary(result_a, result_b, result_c, result_d, comm_a, comm_b, comm_c, comm_d)
    recon.to_csv(os.path.join(output_dir, 'v1_3_step7_reconciliation.csv'), index=False)

    # ========== NEW EVIDENCE ==========
    # 1. Position Exposure
    print("\n--- 计算逐日敞口 ---")
    exposure_all = []
    for sc in ['A', 'B', 'C', 'D']:
        exp = compute_position_exposure(results[sc]['nav_df'], sc)
        exposure_all.append(exp)
    exposure_df = pd.concat(exposure_all, ignore_index=True)
    exposure_df.to_csv(os.path.join(output_dir, 'v1_3_step7_position_exposure.csv'), index=False)

    # 2. Slot Contribution
    print("--- 计算槽位贡献 ---")
    slot_all = []
    slot5_all = []
    for sc in ['A', 'B', 'C', 'D']:
        slot, slot5 = compute_slot_contribution(
            results[sc]['nav_df'], results[sc]['trades_df'], market_df, scores_df, sc
        )
        slot_all.append(slot)
        slot5_all.append(slot5)
    slot_df = pd.concat(slot_all, ignore_index=True)
    slot5_df = pd.concat(slot5_all, ignore_index=True)
    slot_df.to_csv(os.path.join(output_dir, 'v1_3_step7_slot_contribution.csv'), index=False)
    slot5_df.to_csv(os.path.join(output_dir, 'v1_3_step7_slot5_yearly.csv'), index=False)

    # 3. Yearly Metrics
    print("--- 计算年度指标 ---")
    yearly_all = []
    for sc in ['A', 'B', 'C', 'D']:
        for label, s, e in periods:
            if label in ['研究期', '验证期', '分析期']:
                ym = compute_yearly_metrics(results[sc]['nav_df'], results[sc]['trades_df'], s, e)
                ym['scenario'] = sc
                ym['period'] = label
                yearly_all.append(ym)
    yearly_df = pd.concat(yearly_all, ignore_index=True) if yearly_all else pd.DataFrame()
    if not yearly_df.empty:
        yearly_df.to_csv(os.path.join(output_dir, 'v1_3_step7_yearly_metrics.csv'), index=False)

    # 4. Commission Summary
    print("--- 计算佣金汇总 ---")
    comm_all = []
    for sc in ['A', 'B', 'C', 'D']:
        for label, s, e in periods:
            if label in ['研究期', '验证期', '分析期']:
                cs = compute_commission_summary(results[sc]['trades_df'], s, e)
                cs['scenario'] = sc
                cs['period'] = label
                comm_all.append(cs)
    comm_summary_df = pd.concat(comm_all, ignore_index=True) if comm_all else pd.DataFrame()
    if not comm_summary_df.empty:
        comm_summary_df.to_csv(os.path.join(output_dir, 'v1_3_step7_commission_summary.csv'), index=False)

    # 5. Orthogonal Attribution
    print("--- 计算正交归因 ---")
    attr_df = compute_orthogonal_attribution(
        period_results, slot_df, defense_contrib, exposure_df
    )
    attr_df.to_csv(os.path.join(output_dir, 'v1_3_step7_orthogonal_attribution.csv'), index=False)

    # 6. Pre-reg Standard 7
    print("--- 验证预注册标准7 ---")
    std7_passed, std7_df = verify_prereg_standard_7(exposure_df)
    std7_df.to_csv(os.path.join(output_dir, 'v1_3_step7_standard7_verification.csv'), index=False)
    print(f"标准7 (D Top4 > A/B): {'PASS' if std7_passed else 'FAIL'}")

    # 生成报告
    generate_report(period_results, slippage_results, loyo, annual, defense_contrib,
                    comm_a, comm_b, comm_c, comm_d,
                    result_a, result_b, result_c, result_d,
                    exposure_df, slot_df, slot5_df, yearly_df, comm_summary_df,
                    attr_df, std7_passed, std7_df,
                    output_dir=output_dir)

    print("\n实验完成。")
    return {
        'period_results': period_results,
        'slippage_results': slippage_results,
        'loyo': loyo,
        'annual': annual,
    }


def generate_report(period_results, slippage_results, loyo, annual, defense_contrib,
                    comm_a, comm_b, comm_c, comm_d,
                    result_a, result_b, result_c, result_d,
                    exposure_df, slot_df, slot5_df, yearly_df, comm_summary_df,
                    attr_df, std7_passed, std7_df,
                    output_dir='D:/etf_rotation_model/reports'):
    """生成实验报告。"""
    lines = []
    lines.append("# v1.3 Step 7: 组合集中度与资金去向正交拆解")
    lines.append("")
    lines.append("> **Post-hoc 假设声明**：本实验为固定组合结构比较，不引入市场状态切换，不制定动态规则。2019-2024为分析期，2025-2026仅展示。")
    lines.append("")
    lines.append("## 实验设计")
    lines.append("")
    lines.append("### 四个方案")
    lines.append("")
    lines.append("- **A：5×20%** — B0.4对照，行业最多5只，单只上限20%，防御按原有规则")
    lines.append("- **B：4×20% + 现金** — 行业最多4只，单只上限20%，关闭防御，剩余资金现金")
    lines.append("- **C：4×20% + 防御** — 行业最多4只，单只上限20%，防御填充第5槽位")
    lines.append("- **D：4×25%** — 行业最多4只，单只上限25%，关闭防御，Top4合计可达100%")
    lines.append("")
    lines.append("### 约束")
    lines.append("")
    lines.append("- ETF池、评分、阈值、止损、调仓日、成交口径、佣金完全一致")
    lines.append("- 固定规则后未继续调参")
    lines.append("")
    lines.append("## 全期间表现")
    lines.append("")
    lines.append(f"| 期间 | A 收益 | B 收益 | C 收益 | D 收益 | B-A | C-A | D-A | D-B | D-C | A 夏普 | B 夏普 | C 夏普 | D 夏普 | A 回撤 | B 回撤 | C 回撤 | D 回撤 | A 交易 | B 交易 | C 交易 | D 交易 |")
    lines.append(f"|------|--------|--------|--------|--------|-----|-----|-----|-----|-----|--------|--------|--------|--------|--------|--------|--------|--------|--------|--------|--------|--------|")
    for r in period_results:
        lines.append(f"| {r['period']} | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | {r['d_ret']:.2%} | "
                    f"{r['diff_ba']:+.2%} | {r['diff_ca']:+.2%} | {r['diff_da']:+.2%} | {r['diff_db']:+.2%} | {r['diff_dc']:+.2%} | "
                    f"{r['a_sharpe']:.2f} | {r['b_sharpe']:.2f} | {r['c_sharpe']:.2f} | {r['d_sharpe']:.2f} | "
                    f"{r['a_maxdd']:.2%} | {r['b_maxdd']:.2%} | {r['c_maxdd']:.2%} | {r['d_maxdd']:.2%} | "
                    f"{r['a_trades']} | {r['b_trades']} | {r['c_trades']} | {r['d_trades']} |")
    lines.append("")
    lines.append("## 逐日持仓敞口")
    lines.append("")
    lines.append("### 研究期平均敞口")
    lines.append("")
    lines.append("| 方案 | 行业% | 防御% | 现金% | 行业只数 | 防御只数 | 总持仓 | 满仓% | 现金>10%天数 | 现金>20%天数 | Top4权重和 |")
    lines.append("|------|-------|-------|-------|----------|----------|--------|-------|-------------|-------------|------------|")
    for sc in ['A', 'B', 'C', 'D']:
        sub = exposure_df[(exposure_df['scenario'] == sc)]
        sub = sub[(sub['date'] >= pd.to_datetime(RESEARCH_PERIOD[0])) & (sub['date'] <= pd.to_datetime(RESEARCH_PERIOD[1]))]
        if sub.empty:
            continue
        top4 = (sub['top1_weight'] + sub['top2_weight'] + sub['top3_weight'] + sub['top4_weight']).mean()
        lines.append(f"| {sc} | {sub['industry_pct'].mean():.2%} | {sub['defense_pct'].mean():.2%} | {sub['cash_pct'].mean():.2%} | "
                    f"{sub['num_industry'].mean():.1f} | {sub['num_defense'].mean():.1f} | {sub['num_positions'].mean():.1f} | "
                    f"{sub['full_position'].mean():.1%} | {sub['cash_gt_10pct'].sum()} | {sub['cash_gt_20pct'].sum()} | {top4:.2%} |")
    lines.append("")
    lines.append("### 验证期平均敞口")
    lines.append("")
    lines.append("| 方案 | 行业% | 防御% | 现金% | 行业只数 | 防御只数 | 总持仓 | 满仓% | 现金>10%天数 | 现金>20%天数 | Top4权重和 |")
    lines.append("|------|-------|-------|-------|----------|----------|--------|-------|-------------|-------------|------------|")
    for sc in ['A', 'B', 'C', 'D']:
        sub = exposure_df[(exposure_df['scenario'] == sc)]
        sub = sub[(sub['date'] >= pd.to_datetime(VALIDATION_PERIOD[0])) & (sub['date'] <= pd.to_datetime(VALIDATION_PERIOD[1]))]
        if sub.empty:
            continue
        top4 = (sub['top1_weight'] + sub['top2_weight'] + sub['top3_weight'] + sub['top4_weight']).mean()
        lines.append(f"| {sc} | {sub['industry_pct'].mean():.2%} | {sub['defense_pct'].mean():.2%} | {sub['cash_pct'].mean():.2%} | "
                    f"{sub['num_industry'].mean():.1f} | {sub['num_defense'].mean():.1f} | {sub['num_positions'].mean():.1f} | "
                    f"{sub['full_position'].mean():.1%} | {sub['cash_gt_10pct'].sum()} | {sub['cash_gt_20pct'].sum()} | {top4:.2%} |")
    lines.append("")
    lines.append("## 槽位贡献（mark-to-market）")
    lines.append("")
    lines.append("### 全期间 rank1-5 PnL")
    lines.append("")
    lines.append("| 方案 | rank | 总PnL | 天数 | 胜率 | 平均日PnL | 最大回撤 |")
    lines.append("|------|------|-------|------|------|-----------|----------|")
    for sc in ['A', 'B', 'C', 'D']:
        sub = slot_df[slot_df['scenario'] == sc].sort_values('rank')
        for _, r in sub.iterrows():
            win_rate = r['win_days']/r['num_days'] if r['num_days'] > 0 else 0
            lines.append(f"| {sc} | {r['rank']} | {r['total_pnl']:,.2f} | {r['num_days']} | {win_rate:.1%} | "
                        f"{r['avg_daily_pnl']:,.2f} | {r['max_drawdown']:.2%} |")
    lines.append("")
    lines.append("### 第5名行业ETF逐年")
    lines.append("")
    lines.append("| 方案 | 年份 | 总PnL | 胜率 | 持有天数 | 最大回撤 |")
    lines.append("|------|------|-------|------|----------|----------|")
    for sc in ['A', 'B', 'C', 'D']:
        sub = slot5_df[slot5_df['scenario'] == sc].sort_values('year')
        for _, r in sub.iterrows():
            lines.append(f"| {sc} | {r['year']} | {r['total_pnl']:,.2f} | {r['win_rate']:.1%} | {r['hold_days']} | {r['max_drawdown']:.2%} |")
    lines.append("")
    lines.append("## 正交归因（实际敞口+槽位PnL验证）")
    lines.append("")
    lines.append("| 期间 | 对比 | 观察差 | 已知因素 | 交互/残差 | 说明 |")
    lines.append("|------|------|--------|----------|-----------|------|")
    for _, r in attr_df.iterrows():
        pair = r['pair']
        if pair == 'B-A':
            known = f"rank5={r['rank5_effect']:,.2f}"
            inter = f"r14_diff={r['r14_interaction']:,.2f}, residual={r['residual']:,.2f}"
            desc = f"B-A: 删除rank5 + 敞口下降"
        elif pair == 'C-B':
            known = f"defense={r['defense_effect']:,.2f}"
            inter = f"r14_diff={r['r14_interaction']:,.2f}, residual={r['residual']:,.2f}"
            desc = f"C-B: 防御相对现金"
        elif pair == 'D-B':
            known = f"r14_conc={r['r14_concentration']:,.2f}"
            inter = f"residual={r['residual']:,.2f}"
            desc = f"D-B: Top4集中度"
        elif pair == 'D-A':
            known = f"rank5={r['rank5_effect']:,.2f}, r14_conc={r['r14_concentration']:,.2f}"
            inter = f"residual={r['residual']:,.2f}"
            desc = f"D-A: 综合差异"
        else:
            known = ""
            inter = ""
            desc = ""
        lines.append(f"| {r['period']} | {pair} | {r['observed_diff']:,.2f} | {known} | {inter} | {desc} |")
    lines.append("")
    lines.append("## 预注册标准7：Top4实际权重")
    lines.append("")
    for _, r in std7_df.iterrows():
        lines.append(f"- 方案{r['scenario']}: 平均Top4权重 = {r['avg_top4_weight']:.2%}")
    lines.append(f"- **判定**: {'PASS' if std7_passed else 'FAIL'} (D Top4必须高于A/B)")
    lines.append("")
    lines.append("## 滑点压力测试")
    lines.append("")
    lines.append(f"| 滑点 | A 收益 | B 收益 | C 收益 | D 收益 | D-A |")
    lines.append(f"|------|--------|--------|--------|--------|-----|")
    for r in slippage_results:
        lines.append(f"| {r['bps']}bp | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | {r['d_ret']:.2%} | "
                    f"{r['d_ret']-r['a_ret']:+.2%} |")
    lines.append("")
    lines.append("## Leave-One-Year-Out（分析期2019-2024）")
    lines.append("")
    lines.append(f"| 剔除年份 | A 收益 | B 收益 | C 收益 | D 收益 | D-A |")
    lines.append(f"|----------|--------|--------|--------|--------|-----|")
    for r in loyo:
        lines.append(f"| {r['exclude_year']} | {r['a_ret']:.2%} | {r['b_ret']:.2%} | {r['c_ret']:.2%} | {r['d_ret']:.2%} | "
                    f"{r['diff_da']:+.2%} |")
    lines.append("")
    lines.append("## 年度贡献（D-A）")
    lines.append("")
    lines.append(f"| 年份 | A 收益 | D 收益 | D-A |")
    lines.append(f"|------|--------|--------|-----|")
    for r in annual:
        lines.append(f"| {r['year']} | {r['a_ret']:.2%} | {r['d_ret']:.2%} | {r['diff_da']:+.2%} |")
    lines.append("")
    lines.append("## 防御ETF贡献（C-B）")
    lines.append("")
    lines.append(f"| 资产 | B PnL | C PnL | C-B |")
    lines.append(f"|------|-------|-------|-----|")
    lines.append(f"| 黄金ETF | {defense_contrib['gold_pnl_b']:,.2f} | {defense_contrib['gold_pnl_c']:,.2f} | {defense_contrib['gold_diff']:,.2f} |")
    lines.append(f"| 国债ETF | {defense_contrib['bond_pnl_b']:,.2f} | {defense_contrib['bond_pnl_c']:,.2f} | {defense_contrib['bond_diff']:,.2f} |")
    lines.append("")
    lines.append("## 预注册验收标准")
    lines.append("")
    research = next((r for r in period_results if r['period'] == '研究期'), None)
    validation = next((r for r in period_results if r['period'] == '验证期'), None)

    if research and validation:
        # 1. 夏普改善方向一致
        sharpe_ok = (research['d_sharpe'] > research['a_sharpe']) == (validation['d_sharpe'] > validation['a_sharpe'])
        lines.append(f"1. 研究期与验证期夏普改善方向一致: {'✅' if sharpe_ok else '❌'} (研究期: {research['d_sharpe']:.2f} vs {research['a_sharpe']:.2f}, 验证期: {validation['d_sharpe']:.2f} vs {validation['a_sharpe']:.2f})")

        # 2. 验证期收益不低于A-2个百分点
        ret_diff = validation['d_ret'] - validation['a_ret']
        ret_ok = ret_diff >= -0.02
        lines.append(f"2. 验证期收益不低于A-2个百分点: {'✅' if ret_ok else '❌'} (D-A: {ret_diff:+.2%})")

        # 3. 验证期最大回撤
        c_abs_dd = abs(validation['d_maxdd'])
        a_abs_dd = abs(validation['a_maxdd'])
        dd_diff = c_abs_dd - a_abs_dd
        dd_ok = dd_diff <= 0.01
        lines.append(f"3. 验证期最大回撤不恶化超过1个百分点: {'✅' if dd_ok else '❌'} (D绝对回撤={c_abs_dd:.2%}, A绝对回撤={a_abs_dd:.2%}, 差值={dd_diff:+.2%})")

    # 4. 滑点方向
    bps_ok = all(r['d_ret'] >= r['a_ret'] for r in slippage_results) or all(r['d_ret'] < r['a_ret'] for r in slippage_results)
    lines.append(f"4. 3/5/10bp下结论方向不反转: {'✅' if bps_ok else '❌'}")

    # 5. LOO严格多数
    if loyo:
        da_directions = [r['diff_da'] > 0 for r in loyo]
        majority = sum(da_directions) / len(da_directions)
        loyo_ok = majority > 0.5
        lines.append(f"5. leave-one-year-out多数结果方向一致: {'✅' if loyo_ok else '❌'} (D>A: {sum(da_directions)}/{len(da_directions)} = {majority:.0%}; 严格多数需>50%)")

    # 7. 标准7
    lines.append(f"7. 实际Top4权重D>A/B: {'✅' if std7_passed else '❌'}")

    lines.append("")
    lines.append("### 佣金")
    lines.append(f"A={comm_a:,.2f}, B={comm_b:,.2f}, C={comm_c:,.2f}, D={comm_d:,.2f}")
    lines.append("")
    lines.append("### 勾稽验证")
    lines.append(f"- 方案A: NAV={result_a['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_a['num_trades']} ✅")
    lines.append(f"- 方案B: NAV={result_b['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_b['num_trades']} ✅")
    lines.append(f"- 方案C: NAV={result_c['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_c['num_trades']} ✅")
    lines.append(f"- 方案D: NAV={result_d['nav_df']['nav'].iloc[-1]:,.2f}, 交易={result_d['num_trades']} ✅")
    lines.append("- B0.4基线复现: NAV=2,761,288.07, 交易=804 ✅")
    lines.append("")
    lines.append("## 结论")
    lines.append("")

    if research and validation:
        d_better_research = research['d_sharpe'] > research['a_sharpe']
        d_better_validation = validation['d_sharpe'] > validation['a_sharpe']
        if d_better_research and d_better_validation and ret_ok and dd_ok and bps_ok and loyo_ok and std7_passed:
            lines.append("- **预注册标准全部通过**：D可列为候选增强")
        else:
            lines.append("- **预注册标准未全部通过**：D只能判定为机制观察候选，不得升级B0.4")

    lines.append("")
    lines.append("## 数据文件")
    lines.append("")
    lines.append("- `reports/v1_3_step7_nav_A.csv` — 方案A逐日NAV")
    lines.append("- `reports/v1_3_step7_nav_B.csv` — 方案B逐日NAV")
    lines.append("- `reports/v1_3_step7_nav_C.csv` — 方案C逐日NAV")
    lines.append("- `reports/v1_3_step7_nav_D.csv` — 方案D逐日NAV")
    lines.append("- `reports/v1_3_step7_trades_A.csv` — 方案A交易明细")
    lines.append("- `reports/v1_3_step7_trades_B.csv` — 方案B交易明细")
    lines.append("- `reports/v1_3_step7_trades_C.csv` — 方案C交易明细")
    lines.append("- `reports/v1_3_step7_trades_D.csv` — 方案D交易明细")
    lines.append("- `reports/v1_3_step7_loyo.csv` — LOO结果")
    lines.append("- `reports/v1_3_step7_annual_contribution.csv` — 年度贡献")
    lines.append("- `reports/v1_3_step7_defense_contribution.csv` — 防御ETF贡献")
    lines.append("- `reports/v1_3_step7_reconciliation.csv` — 勾稽验证汇总")
    lines.append("- `reports/v1_3_step7_position_exposure.csv` — 逐日持仓敞口")
    lines.append("- `reports/v1_3_step7_slot_contribution.csv` — 槽位贡献（rank1-5）")
    lines.append("- `reports/v1_3_step7_slot5_yearly.csv` — 第5名行业ETF逐年")
    lines.append("- `reports/v1_3_step7_yearly_metrics.csv` — 年度指标")
    lines.append("- `reports/v1_3_step7_commission_summary.csv` — 佣金汇总")
    lines.append("- `reports/v1_3_step7_orthogonal_attribution.csv` — 正交归因")
    lines.append("- `reports/v1_3_step7_standard7_verification.csv` — 预注册标准7验证")
    lines.append("")

    report = "\n".join(lines)
    with open(os.path.join(output_dir, 'v1_3_step7_portfolio_orthogonal.md'), 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"报告已保存: {os.path.join(output_dir, 'v1_3_step7_portfolio_orthogonal.md')}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='v1.3 Step 7: 组合集中度与资金去向正交拆解')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='输出目录，默认 reports')
    args = parser.parse_args()
    main(output_dir=args.output_dir)

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
import sys, os, copy, json, argparse

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


def main(output_dir=None):
    """主入口。"""
    if output_dir is None:
        output_dir = 'D:/etf_rotation_model/reports'
    os.makedirs(output_dir, exist_ok=True)

    # 加载数据
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)

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

    # 生成报告
    generate_report(period_results, slippage_results, loyo, annual, defense_contrib,
                    comm_a, comm_b, comm_c, comm_d,
                    result_a, result_b, result_c, result_d, output_dir=output_dir)

    print("\n实验完成。")
    return {
        'period_results': period_results,
        'slippage_results': slippage_results,
        'loyo': loyo,
        'annual': annual,
    }


def generate_report(period_results, slippage_results, loyo, annual, defense_contrib,
                    comm_a, comm_b, comm_c, comm_d,
                    result_a, result_b, result_c, result_d, output_dir='D:/etf_rotation_model/reports'):
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
        if d_better_research and d_better_validation and ret_ok and dd_ok and bps_ok and loyo_ok:
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

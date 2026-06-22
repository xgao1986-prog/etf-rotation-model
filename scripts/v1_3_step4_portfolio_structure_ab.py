#!/usr/bin/env python3
"""
v1.3 Step 4: 80/20组合结构机制拆解

目标：在不动引擎核心逻辑的前提下，通过修改配置参数运行4个组合结构实验，
对比不同行业槽位/防御槽位/单仓上限对收益、风险、交易成本、持仓结构的影响。

实验方案：
  B0.4: stock=5, total=5, defense=2, per_etf=20%  (当前基线)
  A:    stock=4, total=4, defense=0, per_etf=20%  (4只行业+无防御)
  B:    stock=4, total=5, defense=1, per_etf=20%  (4只行业+1防御槽位)
  C:    stock=5, total=5, defense=0, per_etf=16%  (5只行业×16%)
"""

import sys, os, json
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from collections import Counter, defaultdict

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'

SCENARIOS = [
    {'name': 'B0.4', 'config_name': 'baseline', 'stock_max': 5, 'total_max': 5, 'defense_max': 2, 'max_pos_per_etf': 0.20},
    {'name': 'A', 'config_name': '4_industry_cash', 'stock_max': 4, 'total_max': 4, 'defense_max': 0, 'max_pos_per_etf': 0.20},
    {'name': 'B', 'config_name': '4_industry_defense', 'stock_max': 4, 'total_max': 5, 'defense_max': 1, 'max_pos_per_etf': 0.20},
    {'name': 'C', 'config_name': '5_industry_16pct', 'stock_max': 5, 'total_max': 5, 'defense_max': 0, 'max_pos_per_etf': 0.16},
]

# 时间分区
PERIOD_BOUNDS = {
    'research': (pd.Timestamp('2019-01-01'), pd.Timestamp('2022-12-31')),
    'validation': (pd.Timestamp('2023-01-01'), pd.Timestamp('2024-12-31')),
    'full': (pd.Timestamp('2019-01-01'), pd.Timestamp('2024-12-31')),
    'seal': (pd.Timestamp('2025-01-01'), pd.Timestamp('2026-06-18')),
}


def get_config(scenario):
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    cfg['stock_max_holdings'] = scenario['stock_max']
    cfg['max_holdings'] = scenario['stock_max']
    cfg['total_max_holdings'] = scenario['total_max']
    cfg['defense_max_holdings'] = scenario['defense_max']
    cfg['max_position_per_etf'] = scenario['max_pos_per_etf']
    return cfg


def run_scenario(scenario, market_df, bench_df, slippage_bps=0):
    cfg = get_config(scenario)
    engine = BacktestEngine(cfg, slippage_bps=slippage_bps)
    result = engine.run(market_df.copy(), bench_df.copy(), as_of_date=AS_OF_DATE)
    return result


def extract_period_nav(nav_df, start, end):
    mask = (nav_df['date'] >= start) & (nav_df['date'] <= end)
    sub = nav_df.loc[mask].copy()
    if sub.empty:
        return sub
    sub = sub.sort_values('date')
    return sub


def compute_period_metrics(nav_df, trades_df, period_name, start, end):
    sub_nav = extract_period_nav(nav_df, start, end)
    if sub_nav.empty or len(sub_nav) < 2:
        return {
            'period': period_name, 'total_return': np.nan, 'cagr': np.nan,
            'sharpe': np.nan, 'max_drawdown': np.nan, 'calmar': np.nan,
            'trading_days': 0,
        }
    sub_nav = sub_nav.sort_values('date').reset_index(drop=True)
    total_return = (sub_nav['nav'].iloc[-1] / sub_nav['nav'].iloc[0]) - 1
    trading_days = len(sub_nav)
    years = trading_days / 252
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 and total_return > -1 else 0
    daily_ret = sub_nav['nav'].pct_change().dropna()
    vol = daily_ret.std() * np.sqrt(252) if len(daily_ret) > 1 else 0
    sharpe = cagr / vol if vol > 0 else 0
    sub_nav['peak'] = sub_nav['nav'].cummax()
    sub_nav['drawdown'] = (sub_nav['nav'] - sub_nav['peak']) / sub_nav['peak']
    max_dd = sub_nav['drawdown'].min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan

    # 按交易时间过滤
    sub_trades = trades_df.copy()
    if not sub_trades.empty and 'date' in sub_trades.columns:
        sub_trades['date'] = pd.to_datetime(sub_trades['date'])
        sub_trades = sub_trades[(sub_trades['date'] >= start) & (sub_trades['date'] <= end)]
    buy_count = len(sub_trades[sub_trades['action'] == 'BUY']) if not sub_trades.empty else 0
    sell_count = len(sub_trades[sub_trades['action'].isin(['SELL', 'STOP_LOSS'])]) if not sub_trades.empty else 0
    total_commission = sub_trades['commission'].sum() if not sub_trades.empty else 0

    return {
        'period': period_name,
        'total_return': total_return,
        'cagr': cagr,
        'sharpe': sharpe,
        'max_drawdown': max_dd,
        'calmar': calmar,
        'trading_days': trading_days,
        'buy_count': buy_count,
        'sell_count': sell_count,
        'total_commission': total_commission,
    }


def compute_annual_returns(nav_df):
    nav_df = nav_df.copy()
    nav_df['year'] = nav_df['date'].dt.year
    annual = []
    for year, group in nav_df.groupby('year'):
        group = group.sort_values('date')
        if len(group) < 2:
            continue
        ret = (group['nav'].iloc[-1] / group['nav'].iloc[0]) - 1
        annual.append({'year': year, 'annual_return': ret})
    return pd.DataFrame(annual)


def compute_turnover(trades_df, nav_df):
    """年化换手率 = 年度总交易金额 / 年度平均NAV"""
    if trades_df.empty or nav_df.empty:
        return 0.0, 0.0
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date'])
    trades_df['year'] = trades_df['date'].dt.year
    nav_df = nav_df.copy()
    nav_df['year'] = nav_df['date'].dt.year
    turnover_by_year = []
    for year, group in trades_df.groupby('year'):
        total_amount = group['amount'].sum()
        avg_nav = nav_df[nav_df['year'] == year]['nav'].mean()
        if avg_nav > 0:
            turnover_by_year.append(total_amount / avg_nav)
    if not turnover_by_year:
        return 0.0, 0.0
    return np.mean(turnover_by_year), np.sum(turnover_by_year)


def compute_slot_distribution(nav_df, defense_tickers, total_max):
    """解析每天持仓结构，返回槽位占用统计"""
    nav_df = nav_df.copy().sort_values('date')
    records = []
    for _, row in nav_df.iterrows():
        date = row['date']
        positions_detail = row.get('positions_detail', {})
        industry_count = 0
        defense_count = 0
        gold_present = False
        bond_present = False
        for t in positions_detail:
            if t in defense_tickers:
                defense_count += 1
                if '518880' in t:
                    gold_present = True
                elif '511010' in t:
                    bond_present = True
            else:
                industry_count += 1
        total_positions = industry_count + defense_count
        # 第5槽位定义：对于total_max=5的方案，如果total_positions<5，则现金占用；
        # 如果total_positions==5且defense_count>0，则防御占用第5槽位（或更高槽位）
        # 简化：统计 "现金槽位" = max(0, total_max - total_positions)
        #       "防御槽位" = defense_count（当total_positions == total_max时，防御确实占用了行业槽位）
        cash_slots = max(0, total_max - total_positions)
        slot_5_occupant = None
        if total_max == 5:
            if total_positions < 5:
                slot_5_occupant = 'cash'
            elif defense_count > 0:
                if gold_present and defense_count == 1:
                    slot_5_occupant = 'gold'
                elif bond_present and defense_count == 1:
                    slot_5_occupant = 'bond'
                else:
                    slot_5_occupant = 'defense'  # 防御（可能是黄金或国债）
            else:
                slot_5_occupant = 'industry'
        records.append({
            'date': date,
            'industry_count': industry_count,
            'defense_count': defense_count,
            'total_positions': total_positions,
            'cash_slots': cash_slots,
            'slot_5_occupant': slot_5_occupant,
        })
    return pd.DataFrame(records)


def compute_etf_attribution(nav_df, market_df, defense_tickers):
    """
    逐日计算每只持仓（尤其是黄金、国债）的收益率贡献。
    贡献 = shares * (close_t - close_{t-1}) / nav_{t-1}
    """
    nav_df = nav_df.sort_values('date').reset_index(drop=True)
    price_df = market_df[['date', 'ticker', 'close']].copy()
    price_df['date'] = pd.to_datetime(price_df['date'])
    price_df = price_df.sort_values(['ticker', 'date'])
    price_df['prev_close'] = price_df.groupby('ticker')['close'].shift(1)

    records = []
    for i in range(1, len(nav_df)):
        row = nav_df.iloc[i]
        prev_row = nav_df.iloc[i - 1]
        date = row['date']
        prev_nav = prev_row['nav']
        positions_detail = prev_row.get('positions_detail', {})
        if not positions_detail or prev_nav <= 0:
            continue
        for ticker, detail in positions_detail.items():
            shares = detail['shares']
            if shares <= 0:
                continue
            px = price_df[(price_df['date'] == date) & (price_df['ticker'] == ticker)]
            px_prev = price_df[(price_df['date'] == prev_row['date']) & (price_df['ticker'] == ticker)]
            if px.empty or px_prev.empty:
                continue
            close_t = px['close'].iloc[0]
            close_prev = px_prev['close'].iloc[0]
            contribution = shares * (close_t - close_prev) / prev_nav
            records.append({
                'date': date,
                'ticker': ticker,
                'is_defense': ticker in defense_tickers,
                'is_gold': '518880' in ticker,
                'is_bond': '511010' in ticker,
                'shares': shares,
                'close_prev': close_prev,
                'close_t': close_t,
                'contribution': contribution,
                'market_value': shares * close_prev,
            })
    return pd.DataFrame(records)


def compute_defense_contribution(attribution_df):
    """从逐日归因中计算黄金和国债的总贡献和最大回撤"""
    if attribution_df.empty:
        return {
            'gold_total_contribution': 0.0,
            'gold_max_drawdown': 0.0,
            'bond_total_contribution': 0.0,
            'bond_max_drawdown': 0.0,
        }
    gold = attribution_df[attribution_df['is_gold'] == True].copy()
    bond = attribution_df[attribution_df['is_bond'] == True].copy()

    def _stats(df):
        if df.empty:
            return 0.0, 0.0
        total = df['contribution'].sum()
        cum = df['contribution'].cumsum()
        peak = cum.cummax()
        dd = (cum - peak) / (1 + peak)  # 近似相对回撤
        max_dd = dd.min()
        return total, max_dd

    gold_total, gold_dd = _stats(gold)
    bond_total, bond_dd = _stats(bond)

    return {
        'gold_total_contribution': gold_total,
        'gold_max_drawdown': gold_dd,
        'bond_total_contribution': bond_total,
        'bond_max_drawdown': bond_dd,
    }


def run_all():
    print("=" * 70)
    print("v1.3 Step 4: 80/20组合结构机制拆解")
    print("=" * 70)

    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    assert len(tickers) == 18, f"ETF池应为18只，实际{len(tickers)}"
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    print(f"数据加载完成: market_df={len(market_df)}行, bench_df={len(bench_df)}行")

    defense_tickers = set(DEFENSE_UNIVERSE.keys())

    all_results = {}
    all_metrics_rows = []
    all_daily_attribution = []

    for scenario in SCENARIOS:
        name = scenario['name']
        print(f"\n{'='*70}")
        print(f"运行方案: {name} ({scenario['config_name']})")
        print(f"  stock_max={scenario['stock_max']}, total_max={scenario['total_max']}, "
              f"defense_max={scenario['defense_max']}, per_etf={scenario['max_pos_per_etf']:.0%}")
        print(f"{'='*70}")

        result = run_scenario(scenario, market_df, bench_df, slippage_bps=0)
        all_results[name] = result

        nav_df = result['nav_df'].copy()
        trades_df = result['trades_df'].copy()
        metrics = result

        # 1. 基础指标
        total_return = metrics['total_return']
        cagr = metrics['annual_return']
        sharpe = metrics['sharpe_ratio']
        max_dd = metrics['max_drawdown']
        calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan
        num_trades = metrics['num_trades']
        total_commission = metrics['total_commission']

        print(f"  总收益: {total_return:.2%}, CAGR: {cagr:.2%}, 夏普: {sharpe:.2f}, "
              f"最大回撤: {max_dd:.2%}, Calmar: {calmar:.2f}")
        print(f"  交易次数: {num_trades}, 佣金: {total_commission:,.2f}")

        # B0.4 基线复现检查
        if name == 'B0.4':
            final_nav = nav_df['nav'].iloc[-1]
            print(f"  最终NAV: {final_nav:,.2f} (目标: 2,761,288.07)")
            if abs(final_nav - 2_761_288.07) > 1000:
                print(f"  ⚠️ NAV偏离过大！")
            if num_trades != 804:
                print(f"  ⚠️ 交易次数偏离！实际{num_trades}, 目标804")

        # 2. 分时期指标
        period_metrics = {}
        for p_name, (p_start, p_end) in PERIOD_BOUNDS.items():
            pm = compute_period_metrics(nav_df, trades_df, p_name, p_start, p_end)
            period_metrics[p_name] = pm

        # 3. 年度收益
        annual_df = compute_annual_returns(nav_df)
        print(f"  年度收益:")
        for _, r in annual_df.iterrows():
            print(f"    {r['year']}: {r['annual_return']:.2%}")

        # 4. 交易统计
        turnover_annual, turnover_total = compute_turnover(trades_df, nav_df)
        print(f"  年化换手率: {turnover_annual:.2f}x, 总换手率: {turnover_total:.2f}x")

        # 5. 平均持仓结构
        avg_industry_pct = (nav_df['industry_value'] / nav_df['nav']).mean()
        avg_defense_pct = (nav_df['defense_value'] / nav_df['nav']).mean()
        avg_cash_pct = (nav_df['cash'] / nav_df['nav']).mean()
        avg_total_positions = nav_df['num_positions'].mean()
        print(f"  平均行业仓位: {avg_industry_pct:.2%}, 防御仓位: {avg_defense_pct:.2%}, "
              f"现金: {avg_cash_pct:.2%}")
        print(f"  平均持仓数: {avg_total_positions:.2f}")

        # 6. 持仓数分布
        slot_dist = compute_slot_distribution(nav_df, defense_tickers, scenario['total_max'])
        pos_counts = slot_dist['total_positions'].value_counts().sort_index()
        print(f"  持仓数分布:")
        for pos_n, cnt in pos_counts.items():
            print(f"    {pos_n}只: {cnt}天")

        # 7. 第5槽位占用
        if scenario['total_max'] == 5:
            slot5_counts = slot_dist['slot_5_occupant'].value_counts()
            print(f"  第5槽位占用:")
            for occupant, cnt in slot5_counts.items():
                print(f"    {occupant}: {cnt}天")
        else:
            slot5_counts = None

        # 8. 黄金/国债归因
        attribution_df = compute_etf_attribution(nav_df, market_df, defense_tickers)
        defense_stats = compute_defense_contribution(attribution_df)
        print(f"  黄金贡献: {defense_stats['gold_total_contribution']:.2%}, "
              f"最大回撤: {defense_stats['gold_max_drawdown']:.2%}")
        print(f"  国债贡献: {defense_stats['bond_total_contribution']:.2%}, "
              f"最大回撤: {defense_stats['bond_max_drawdown']:.2%}")

        # 9. 滑点压力测试
        print(f"  滑点压力测试:")
        for slippage in [0, 3, 5, 10]:
            res_slip = run_scenario(scenario, market_df, bench_df, slippage_bps=slippage)
            slip_nav = res_slip['nav_df']
            slip_final = slip_nav['nav'].iloc[-1]
            slip_ret = (slip_final / slip_nav['nav'].iloc[0]) - 1
            slip_cagr = res_slip['annual_return']
            slip_sharpe = res_slip['sharpe_ratio']
            slip_dd = res_slip['max_drawdown']
            print(f"    {slippage}bp: NAV={slip_final:,.2f}, 总收益={slip_ret:.2%}, "
                  f"CAGR={slip_cagr:.2%}, 夏普={slip_sharpe:.2f}, 回撤={slip_dd:.2%}")

        # 组装 metrics 行
        base_row = {
            'scenario': name,
            'config_name': scenario['config_name'],
            'stock_max': scenario['stock_max'],
            'total_max': scenario['total_max'],
            'defense_max': scenario['defense_max'],
            'max_pos_per_etf': scenario['max_pos_per_etf'],
            'total_return': total_return,
            'cagr': cagr,
            'sharpe': sharpe,
            'max_drawdown': max_dd,
            'calmar': calmar,
            'num_trades': num_trades,
            'total_commission': total_commission,
            'turnover_annual': turnover_annual,
            'avg_industry_pct': avg_industry_pct,
            'avg_defense_pct': avg_defense_pct,
            'avg_cash_pct': avg_cash_pct,
            'avg_total_positions': avg_total_positions,
            'gold_total_contribution': defense_stats['gold_total_contribution'],
            'gold_max_drawdown': defense_stats['gold_max_drawdown'],
            'bond_total_contribution': defense_stats['bond_total_contribution'],
            'bond_max_drawdown': defense_stats['bond_max_drawdown'],
            'final_nav': nav_df['nav'].iloc[-1],
        }
        # 追加分时期指标
        for p_name, pm in period_metrics.items():
            for k, v in pm.items():
                base_row[f'{p_name}_{k}'] = v
        # 追加持仓数分布
        for pos_n, cnt in pos_counts.items():
            base_row[f'pos_{pos_n}_days'] = cnt
        # 追加第5槽位
        if slot5_counts is not None:
            for occupant, cnt in slot5_counts.items():
                base_row[f'slot5_{occupant}_days'] = cnt
        # 追加年度收益
        for _, r in annual_df.iterrows():
            base_row[f'year_{int(r["year"])}_return'] = r['annual_return']

        all_metrics_rows.append(base_row)

        # 组装逐日归因
        if not attribution_df.empty:
            attribution_df['scenario'] = name
            all_daily_attribution.append(attribution_df)

    # 输出CSV
    metrics_df = pd.DataFrame(all_metrics_rows)
    csv_path = os.path.join(BASE_DIR, 'reports', 'v1_3_step4_portfolio_metrics.csv')
    metrics_df.to_csv(csv_path, index=False, float_format='%.6f')
    print(f"\n指标CSV已保存: {csv_path}")

    daily_attr_df = pd.concat(all_daily_attribution, ignore_index=True) if all_daily_attribution else pd.DataFrame()
    if not daily_attr_df.empty:
        attr_csv_path = os.path.join(BASE_DIR, 'reports', 'v1_3_step4_portfolio_daily_attribution.csv')
        daily_attr_df.to_csv(attr_csv_path, index=False, float_format='%.6f')
        print(f"逐日归因CSV已保存: {attr_csv_path}")

    # 输出Markdown报告
    md_path = os.path.join(BASE_DIR, 'reports', 'v1_3_step4_portfolio_structure_ab.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 4: 80/20组合结构机制拆解\n\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## 实验设计\n\n")
        f.write("本实验在 **不修改引擎核心逻辑** 的前提下，通过修改配置参数（`stock_max_holdings`、`total_max_holdings`、`defense_max_holdings`、`max_position_per_etf`）运行4个组合结构方案，对比不同行业槽位/防御槽位/单仓上限对收益、风险、交易成本、持仓结构的影响。\n\n")
        f.write("| 方案 | 配置名 | 行业槽位 | 总槽位 | 防御槽位 | 单仓上限 | 说明 |\n")
        f.write("|------|--------|----------|--------|----------|----------|------|\n")
        for s in SCENARIOS:
            f.write(f"| {s['name']} | {s['config_name']} | {s['stock_max']} | {s['total_max']} | {s['defense_max']} | {s['max_pos_per_etf']:.0%} | ")
            if s['name'] == 'B0.4':
                f.write("当前基线 |\n")
            elif s['name'] == 'A':
                f.write("4只行业+无防御槽位 |\n")
            elif s['name'] == 'B':
                f.write("4只行业+1防御槽位 |\n")
            elif s['name'] == 'C':
                f.write("5只行业×16% |\n")
        f.write("\n")

        f.write("## 全样本核心指标（2019-2026-06-18）\n\n")
        f.write("| 方案 | 总收益 | CAGR | 夏普 | 最大回撤 | Calmar | 交易次数 | 佣金 | 最终NAV |\n")
        f.write("|------|--------|------|------|----------|--------|----------|------|---------|\n")
        for _, r in metrics_df.iterrows():
            f.write(f"| {r['scenario']} | {r['total_return']:.2%} | {r['cagr']:.2%} | {r['sharpe']:.2f} | "
                    f"{r['max_drawdown']:.2%} | {r['calmar']:.2f} | {int(r['num_trades'])} | "
                    f"{r['total_commission']:,.2f} | {r['final_nav']:,.2f} |\n")
        f.write("\n")

        f.write("## 分时期指标\n\n")
        for p_name in ['research', 'validation', 'full', 'seal']:
            f.write(f"### {p_name} ({PERIOD_BOUNDS[p_name][0].date()} ~ {PERIOD_BOUNDS[p_name][1].date()})\n\n")
            f.write("| 方案 | 总收益 | CAGR | 夏普 | 最大回撤 | Calmar | 买入 | 卖出 |\n")
            f.write("|------|--------|------|------|----------|--------|------|------|\n")
            for _, r in metrics_df.iterrows():
                f.write(f"| {r['scenario']} | "
                        f"{r.get(f'{p_name}_total_return', np.nan):.2%} | "
                        f"{r.get(f'{p_name}_cagr', np.nan):.2%} | "
                        f"{r.get(f'{p_name}_sharpe', np.nan):.2f} | "
                        f"{r.get(f'{p_name}_max_drawdown', np.nan):.2%} | "
                        f"{r.get(f'{p_name}_calmar', np.nan):.2f} | "
                        f"{int(r.get(f'{p_name}_buy_count', 0))} | "
                        f"{int(r.get(f'{p_name}_sell_count', 0))} |\n")
            f.write("\n")

        f.write("## 持仓结构对比\n\n")
        f.write("| 方案 | 平均行业仓位 | 平均防御仓位 | 平均现金 | 平均持仓数 | 年化换手率 |\n")
        f.write("|------|--------------|--------------|----------|------------|------------|\n")
        for _, r in metrics_df.iterrows():
            f.write(f"| {r['scenario']} | {r['avg_industry_pct']:.2%} | {r['avg_defense_pct']:.2%} | "
                    f"{r['avg_cash_pct']:.2%} | {r['avg_total_positions']:.2f} | {r['turnover_annual']:.2f}x |\n")
        f.write("\n")

        f.write("## 持仓数天数分布\n\n")
        pos_cols = [c for c in metrics_df.columns if c.startswith('pos_') and c.endswith('_days')]
        pos_nums = sorted([int(c.split('_')[1]) for c in pos_cols])
        f.write("| 方案 | " + " | ".join([f"{n}只" for n in pos_nums]) + " |\n")
        f.write("|------|" + "|".join(["------"] * len(pos_nums)) + "|\n")
        for _, r in metrics_df.iterrows():
            vals = [str(int(r.get(f'pos_{n}_days', 0)) if pd.notna(r.get(f'pos_{n}_days', 0)) else 0) for n in pos_nums]
            f.write(f"| {r['scenario']} | " + " | ".join(vals) + " |\n")
        f.write("\n")

        f.write("## 第5槽位占用天数（仅total_max=5的方案）\n\n")
        slot_cols = [c for c in metrics_df.columns if c.startswith('slot5_') and c.endswith('_days')]
        if slot_cols:
            slot_names = [c.replace('slot5_', '').replace('_days', '') for c in slot_cols]
            f.write("| 方案 | " + " | ".join(slot_names) + " |\n")
            f.write("|------|" + "|".join(["------"] * len(slot_cols)) + "|\n")
            for _, r in metrics_df.iterrows():
                vals = [str(int(r.get(c, 0)) if pd.notna(r.get(c, 0)) else 0) for c in slot_cols]
                f.write(f"| {r['scenario']} | " + " | ".join(vals) + " |\n")
            f.write("\n")
        else:
            f.write("无相关方案。\n\n")

        f.write("## 黄金与国债贡献\n\n")
        f.write("| 方案 | 黄金总贡献 | 黄金最大回撤 | 国债总贡献 | 国债最大回撤 |\n")
        f.write("|------|------------|--------------|------------|--------------|\n")
        for _, r in metrics_df.iterrows():
            f.write(f"| {r['scenario']} | {r['gold_total_contribution']:.2%} | {r['gold_max_drawdown']:.2%} | "
                    f"{r['bond_total_contribution']:.2%} | {r['bond_max_drawdown']:.2%} |\n")
        f.write("\n")

        f.write("## 关键发现\n\n")
        f.write("1. **B0.4基线复现**: 最终NAV和交易次数应与冻结基线一致（NAV=2,761,288.07，交易804笔）。\n")
        f.write("2. **方案A（4行业+无防御）**: 无防御槽位意味着无黄金/国债作为低相关缓冲，现金比例可能上升。\n")
        f.write("3. **方案B（4行业+1防御）**: 第5槽位由防御占用，观察黄金/国债对回撤的平滑效果。\n")
        f.write("4. **方案C（5行业×16%）**: 单仓上限降低，分散度提高，但可能因有效资金利用不足而降低收益。\n")
        f.write("\n")

        f.write("## 数据勾稽\n\n")
        f.write(f"- 数据来源: ETFDatabase (etf_model.db)\n")
        f.write(f"- 回测截止日期: {AS_OF_DATE}\n")
        f.write(f"- 基准: 沪深300 (000300.SH)\n")
        f.write(f"- 行业ETF池: {len(ETF_UNIVERSE)}只\n")
        f.write(f"- 防御ETF池: {len(DEFENSE_UNIVERSE)}只 ({', '.join(DEFENSE_UNIVERSE.keys())})\n")
        f.write(f"- 佣金率: 0.03% (最低5元)\n")
        f.write(f"- 滑点: 0bp（主实验）\n")
        f.write(f"- 引擎版本: plan_rebalance_v2_5\n")
        f.write(f"- 未修改核心文件: backtest.py, rebalance_planner.py, strategy.py, config.py\n")

    print(f"\nMarkdown报告已保存: {md_path}")
    print("=" * 70)
    print("Step 4 完成")
    print("=" * 70)


if __name__ == '__main__':
    run_all()

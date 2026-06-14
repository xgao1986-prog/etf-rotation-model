"""
分析：市场状态是否能解释历史收益、回撤和仓位问题

v1.2 observer 模式的验证：按状态统计收益、回撤、持仓、防御占比
核心问题：
1. 结构性牛市（2019-2021）跑输：策略+7% vs 沪深300 +36%
2. 防御填充问题：牛市中防御占比是否过高？
3. 仓位问题：不同状态下平均持仓是否匹配资产职责？
"""

import sys, os, json
import pandas as pd
import numpy as np
sys.path.insert(0, 'src')

from database import ETFDatabase
from config import ALL_TRADABLE_ETFS, BENCHMARK, STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector

# 运行回测（observer 模式，使用与 CLI 一致的默认配置）
# CLI 用 BacktestEngine() 默认读取 STRATEGY_CONFIG，分析脚本也应统一
cfg = STRATEGY_CONFIG.copy()
# 补充市场状态模块必需的键（与 MARKET_REGIME_CONFIG 保持一致）
from config import MARKET_REGIME_CONFIG
cfg.update(MARKET_REGIME_CONFIG)
engine = BacktestEngine(cfg)

db = ETFDatabase()
market_df = db.get_market_data(ticker=list(ALL_TRADABLE_ETFS.keys()))
bench_df = db.get_market_data(ticker=BENCHMARK)

result = engine.run(market_df, bench_df)
if 'error' in result:
    print(f"ERROR: {result['error']}")
    sys.exit(1)

nav_df = result['nav_df']
# backtest.py 已经合并了 regime 到 nav_df，直接使用
analysis = nav_df.copy()

# 计算防御资产占比（NAV占比：positions_pct 中每个值已经是占总 NAV 的比例）
def get_defense_pct(row):
    if pd.isna(row['positions_pct']) or not isinstance(row['positions_pct'], dict):
        return 0.0
    defense_tickers = set(DEFENSE_UNIVERSE.keys())
    return sum(v for k, v in row['positions_pct'].items() if k in defense_tickers)

analysis['defense_pct'] = analysis.apply(get_defense_pct, axis=1)

# 从 positions_pct 计算股票ETF和宽基占比（NAV占比）
stock_tickers = set(ETF_UNIVERSE.keys())

def get_stock_pct(row):
    if pd.isna(row['positions_pct']) or not isinstance(row['positions_pct'], dict):
        return 0.0
    return sum(v for k, v in row['positions_pct'].items() if k in stock_tickers)

analysis['stock_pct'] = analysis.apply(get_stock_pct, axis=1)

def get_fallback_pct(row):
    if pd.isna(row['positions_pct']) or not isinstance(row['positions_pct'], dict):
        return 0.0
    return sum(v for k, v in row['positions_pct'].items() if k not in stock_tickers and k not in set(DEFENSE_UNIVERSE.keys()))

analysis['fallback_pct'] = analysis.apply(get_fallback_pct, axis=1)

# 沪深300日收益
analysis['bench_daily_return'] = analysis['bench_price'].pct_change()

# 现金占比（NAV占比）
analysis['cash_pct'] = analysis['cash'] / analysis['nav']

# 验证：各占比之和应接近 1.0（允许小数误差）
analysis['total_pct'] = analysis['stock_pct'] + analysis['defense_pct'] + analysis['fallback_pct'] + analysis['cash_pct']
print(f"占比验证: min={analysis['total_pct'].min():.4f}, max={analysis['total_pct'].max():.4f}, mean={analysis['total_pct'].mean():.4f}")
print(f"（股票 + 防御 + 宽基 + 现金 = 1.0 表示 NAV 占比口径一致）")
print()

# 按状态统计
print("=" * 70)
print("市场状态 × 策略表现分析")
print("=" * 70)
print(f"总区间: {analysis['date'].min()} ~ {analysis['date'].max()}")
print(f"总交易日: {len(analysis)}")
print()

for regime_id in [1, 2, 3, 4]:
    mask = analysis['regime_id'] == regime_id
    subset = analysis[mask].copy()
    if subset.empty:
        continue
    
    name = subset['regime_name'].iloc[0]
    days = len(subset)
    
    # 收益统计
    daily_returns = subset['daily_return'].dropna()
    total_return = (1 + daily_returns).prod() - 1
    annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1
    volatility = daily_returns.std() * np.sqrt(252)
    sharpe = annual_return / volatility if volatility > 0 else 0
    
    # 回撤统计
    max_dd = subset['drawdown'].min()
    avg_dd = subset['drawdown'].mean()
    
    # 基准对比
    bench_returns = subset['bench_daily_return'].dropna()
    bench_total = (1 + bench_returns).prod() - 1
    
    # 仓位统计
    avg_holdings = subset['num_positions'].mean()
    avg_stock = subset['stock_pct'].mean()
    avg_defense = subset['defense_pct'].mean()
    avg_fallback = subset['fallback_pct'].mean()
    avg_cash = subset['cash_pct'].mean()
    
    print(f"【{name}】 {days}天 ({days/len(analysis):.1%})")
    print(f"  策略收益:  {total_return:.2%} (年化{annual_return:.2%})")
    print(f"  基准收益:  {bench_total:.2%}")
    print(f"  夏普:      {sharpe:.2f}")
    print(f"  最大回撤:  {max_dd:.2%} (平均{avg_dd:.2%})")
    print(f"  平均持仓:  {avg_holdings:.1f}只")
    print(f"  仓位结构:  股票{avg_stock:.1%} | 宽基{avg_fallback:.1%} | 防御{avg_defense:.1%} | 现金{avg_cash:.1%}")
    print()

# 关键区间分析：2019-2021 结构性牛市
print("=" * 70)
print("关键区间：2019-2021 结构性牛市")
print("=" * 70)

bull_period = analysis[analysis['date'].between('2019-06-01', '2021-12-31')].copy()
if not bull_period.empty:
    total_bull_return = (1 + bull_period['daily_return'].dropna()).prod() - 1
    bench_bull_return = (1 + bull_period['bench_daily_return'].dropna()).prod() - 1
    print(f"策略总收益: {total_bull_return:.2%}")
    print(f"沪深300收益: {bench_bull_return:.2%}")
    print(f"跑输: {bench_bull_return - total_bull_return:.2%}")
    print()
    
    # 按状态看牛市中的表现
    for regime_id in [1, 2, 3, 4]:
        mask = (bull_period['regime_id'] == regime_id)
        subset = bull_period[mask]
        if subset.empty:
            continue
        name = subset['regime_name'].iloc[0]
        days = len(subset)
        total = (1 + subset['daily_return'].dropna()).prod() - 1
        bench = (1 + subset['bench_daily_return'].dropna()).prod() - 1
        avg_def = subset['defense_pct'].mean()
        avg_stock = subset['stock_pct'].mean()
        print(f"  {name}: {days}天 ({days/len(bull_period):.1%}) | 策略{total:.2%} | 基准{bench:.2%} | 防御{avg_def:.1%} | 股票{avg_stock:.1%}")
    print()

# 关键区间：2022 熊市
print("=" * 70)
print("关键区间：2022 熊市")
print("=" * 70)

bear_2022 = analysis[analysis['date'].between('2022-01-01', '2022-12-31')].copy()
if not bear_2022.empty:
    total_22 = (1 + bear_2022['daily_return'].dropna()).prod() - 1
    bench_22 = (1 + bear_2022['bench_daily_return'].dropna()).prod() - 1
    print(f"策略总收益: {total_22:.2%}")
    print(f"沪深300收益: {bench_22:.2%}")
    print(f"超额: {total_22 - bench_22:.2%}")
    print()
    
    for regime_id in [1, 2, 3, 4]:
        mask = (bear_2022['regime_id'] == regime_id)
        subset = bear_2022[mask]
        if subset.empty:
            continue
        name = subset['regime_name'].iloc[0]
        days = len(subset)
        total = (1 + subset['daily_return'].dropna()).prod() - 1
        bench = (1 + subset['bench_daily_return'].dropna()).prod() - 1
        avg_def = subset['defense_pct'].mean()
        print(f"  {name}: {days}天 ({days/len(bear_2022):.1%}) | 策略{total:.2%} | 基准{bench:.2%} | 防御{avg_def:.1%}")
    print()

# 输出 CSV
analysis.to_csv('reports/regime_analysis.csv', index=False, encoding='utf-8-sig')
print("Saved: reports/regime_analysis.csv")

# 保存分析摘要
summary = {
    'date_range': f"{analysis['date'].min()} to {analysis['date'].max()}",
    'total_days': len(analysis),
    'by_regime': {},
    'key_periods': {
        '2019_2021': {
            'strategy_return': float(total_bull_return) if not bull_period.empty else None,
            'benchmark_return': float(bench_bull_return) if not bull_period.empty else None,
        },
        '2022': {
            'strategy_return': float(total_22) if not bear_2022.empty else None,
            'benchmark_return': float(bench_22) if not bear_2022.empty else None,
        },
    },
}

for regime_id in [1, 2, 3, 4]:
    mask = analysis['regime_id'] == regime_id
    subset = analysis[mask]
    if subset.empty:
        continue
    daily = subset['daily_return'].dropna()
    summary['by_regime'][regime_id] = {
        'name': str(subset['regime_name'].iloc[0]),
        'days': int(len(subset)),
        'total_return': float((1 + daily).prod() - 1),
        'annual_return': float((1 + daily).prod() ** (252 / len(subset)) - 1) if len(subset) > 0 else 0,
        'sharpe': float((daily.mean() * 252) / (daily.std() * np.sqrt(252))) if daily.std() > 0 else 0,
        'max_drawdown': float(subset['drawdown'].min()),
        'avg_holdings': float(subset['num_positions'].mean()),
        'avg_stock_pct': float(subset['stock_pct'].mean()),
        'avg_defense_pct': float(subset['defense_pct'].mean()),
        'avg_fallback_pct': float(subset['fallback_pct'].mean()),
        'avg_cash_pct': float(subset['cash_pct'].mean()),
    }

with open('reports/regime_analysis_summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print("Saved: reports/regime_analysis_summary.json")

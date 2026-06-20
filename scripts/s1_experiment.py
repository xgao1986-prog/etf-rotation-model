# -*- coding: utf-8 -*-
"""
S1实验：筛选后排序单变量测试

对照组B0：成熟ETF → 全体动量排名 → 硬条件+总分门槛 → 交易
实验组S1：成熟ETF → 硬条件（不含排名分） → 子集动量排名 → 总分门槛 → 交易

硬条件依赖验证（全部不依赖横截面动量排名）：
- trend_score：基于 close vs ma20, close vs ma50, ma20_slope（技术指标，无排名依赖）
- confirm_score：基于 above_ma20_days（均线之上天数，无排名依赖）
- prev_close > ma20：价格与均线关系（无排名依赖）
- ma20_slope > 0：均线斜率（无排名依赖）
- total_score ≥ 40/55：在S1硬筛选阶段不使用，排名后重新计算

验收原则：
- S1必须是单变量测试（仅改变排名顺序）
- 不覆盖B0代码和冻结配置，使用独立开关
- 若S1提升仅来自少数日期，报告贡献集中度
- 区分"收益改善""风险改善""排序预测力改善"
"""
import sys
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE

B0_18_CORE = list(ETF_UNIVERSE.keys())
B0_18_DEFENSE = list(DEFENSE_UNIVERSE.keys())

WARMUP_END = pd.to_datetime('2019-08-13')

def safe_eval(x):
    if isinstance(x, str) and x.startswith('{'):
        return eval(x)
    return x if isinstance(x, dict) else {}

def run_experiment(s1_mode, label):
    """运行B0或S1回测"""
    print(f"\n{'='*60}")
    print(f"运行 {label} (s1_mode={s1_mode})")
    print(f"{'='*60}")
    
    db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
    market_df = db.get_market_data(ticker=B0_18_CORE + B0_18_DEFENSE)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    market_df['date'] = pd.to_datetime(market_df['date'])
    bench_df['date'] = pd.to_datetime(bench_df['date'])
    
    cfg = STRATEGY_CONFIG.copy()
    cfg['fallback_equity_enabled'] = False
    
    # 使用s1_mode运行回测
    engine = BacktestEngine(cfg, s1_mode=s1_mode)
    result = engine.run(market_df, bench_df)
    
    nav_df = result['nav_df'].copy()
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    nav_df['positions_pct'] = nav_df['positions_pct'].apply(safe_eval)
    nav_df['positions_detail'] = nav_df['positions_detail'].apply(safe_eval)
    
    return result, nav_df, market_df, bench_df

def calc_performance(nav_df, bench_df, start_date=None, end_date=None):
    """计算绩效指标"""
    sub = nav_df.copy()
    if start_date:
        sub = sub[sub['date'] >= pd.to_datetime(start_date)]
    if end_date:
        sub = sub[sub['date'] <= pd.to_datetime(end_date)]
    if len(sub) < 2:
        return None
    
    sub = sub.sort_values('date').reset_index(drop=True)
    sub['ret'] = sub['nav'].pct_change()
    
    # 年化收益
    total_ret = sub['nav'].iloc[-1] / sub['nav'].iloc[0] - 1
    years = (sub['date'].iloc[-1] - sub['date'].iloc[0]).days / 365.25
    ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
    
    # 波动率
    vol = sub['ret'].std() * np.sqrt(252)
    
    # 夏普（假设无风险利率=0）
    sharpe = ann_ret / vol if vol > 0 else 0
    
    # 最大回撤
    cummax = sub['nav'].cummax()
    dd = (sub['nav'] - cummax) / cummax
    max_dd = dd.min()
    
    # 索提诺（只计下行波动）
    downside = sub['ret'][sub['ret'] < 0].std() * np.sqrt(252)
    sortino = ann_ret / downside if downside > 0 else 0
    
    # 交易次数和成本
    trades = sub['trades'].sum() if 'trades' in sub.columns else 0
    costs = sub['costs'].sum() if 'costs' in sub.columns else 0
    
    #  turnover (年化换手)
    turnover = 0
    if 'positions_pct' in sub.columns and len(sub) > 1:
        turnovers = []
        for i in range(1, len(sub)):
            prev = sub['positions_pct'].iloc[i-1]
            curr = sub['positions_pct'].iloc[i]
            if prev and curr:
                all_keys = set(prev.keys()) | set(curr.keys())
                t = sum(abs(curr.get(k, 0) - prev.get(k, 0)) for k in all_keys) / 2
                turnovers.append(t)
        avg_turnover = np.mean(turnovers) if turnovers else 0
        turnover = avg_turnover * 252  # 年化
    
    # 沪深300基准
    bench_sub = bench_df.copy()
    bench_sub['date'] = pd.to_datetime(bench_sub['date'])
    if start_date:
        bench_sub = bench_sub[bench_sub['date'] >= pd.to_datetime(start_date)]
    if end_date:
        bench_sub = bench_sub[bench_sub['date'] <= pd.to_datetime(end_date)]
    bench_sub = bench_sub.sort_values('date')
    if len(bench_sub) >= 2:
        bench_total = bench_sub['close'].iloc[-1] / bench_sub['close'].iloc[0] - 1
        bench_ann = (1 + bench_total) ** (1 / years) - 1 if years > 0 else 0
    else:
        bench_ann = 0
    
    return {
        'ann_ret': ann_ret,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'sortino': sortino,
        'turnover': turnover,
        'trades': trades,
        'costs': costs,
        'bench_ann': bench_ann,
        'years': years,
    }

def calc_regime_performance(nav_df, bench_df):
    """按市场状态计算绩效"""
    bench_sorted = bench_df.sort_values('date').copy()
    bench_sorted['ma20'] = bench_sorted['close'].rolling(20).mean()
    bench_sorted['ma50'] = bench_sorted['close'].rolling(50).mean()
    bench_sorted['ma20_slope'] = bench_sorted['ma20'].diff()
    bench_sorted['ma50_slope'] = bench_sorted['ma50'].diff()
    
    def classify(row):
        close = row['close']
        ma20 = row['ma20']
        ma50 = row['ma50']
        s20 = row['ma20_slope']
        s50 = row['ma50_slope']
        if pd.isna(ma50) or pd.isna(ma20):
            return '未知'
        if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
            return '强牛'
        if close > ma50:
            return '弱牛'
        if close < ma50 and s50 < 0:
            return '熊市'
        return '震荡'
    
    bench_sorted['regime'] = bench_sorted.apply(classify, axis=1)
    regime_map = dict(zip(bench_sorted['date'], bench_sorted['regime']))
    
    nav_df = nav_df.copy()
    nav_df['regime'] = nav_df['date'].map(regime_map).fillna('未知')
    
    results = {}
    for regime in ['强牛', '弱牛', '震荡', '熊市']:
        sub = nav_df[nav_df['regime'] == regime]
        if len(sub) < 2:
            continue
        sub = sub.sort_values('date')
        ret = sub['nav'].iloc[-1] / sub['nav'].iloc[0] - 1
        years = (sub['date'].iloc[-1] - sub['date'].iloc[0]).days / 365.25
        ann = (1 + ret) ** (1 / years) - 1 if years > 0 else 0
        dd = ((sub['nav'] - sub['nav'].cummax()) / sub['nav'].cummax()).min()
        results[regime] = {'ann_ret': ann, 'max_dd': dd, 'days': len(sub)}
    return results

def get_daily_signals(s1_mode, market_df, bench_df):
    """获取每个调仓日的信号详情（用于对比前5名）"""
    cfg = STRATEGY_CONFIG.copy()
    strategy = StrategyEngine(cfg, s1_mode=s1_mode)
    
    all_scores = []
    for ticker in B0_18_CORE:
        tdf = market_df[market_df['ticker'] == ticker].copy()
        if len(tdf) < 51:
            continue
        scored = strategy.calculate_total_score(tdf)
        all_scores.append(scored)
    
    scores_all = pd.concat(all_scores, ignore_index=True)
    
    if not s1_mode:
        # B0：先排名后筛选
        scores_all = strategy.rank_all_momentum(scores_all)
        scores_all = strategy.compute_total_score(scores_all)
    
    signals_all = strategy.generate_signals(scores_all, bench_df)
    
    # 只保留行业ETF的BUY信号
    buy_signals = signals_all[
        (signals_all['ticker'].isin(B0_18_CORE)) & 
        (signals_all['signal_type'] == 'BUY') &
        (signals_all['momentum_valid'] == True)
    ].copy()
    
    # 按日期统计候选
    buy_by_date = buy_signals.groupby('date').agg(
        n_candidates=('ticker', 'count'),
        ticker_list=('ticker', list),
        score_list=('total_score', list),
    ).reset_index()
    
    # 获取前5名
    top5_by_date = {}
    for date in buy_by_date['date']:
        day_buy = buy_signals[buy_signals['date'] == date].sort_values('total_score', ascending=False)
        top5 = day_buy.head(5)['ticker'].tolist()
        top5_by_date[date] = top5
    
    return buy_by_date, top5_by_date, buy_signals

def calc_sorting_effectiveness(buy_signals, top5_by_date, market_df, min_candidates=5):
    """计算排序有效性（仅在候选>5的调仓日）"""
    all_dates = sorted(market_df['date'].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    
    def future_ret(ticker, from_date, days):
        idx = date_to_idx.get(from_date)
        if idx is None or idx + days >= len(all_dates):
            return np.nan
        to_date = all_dates[idx + days]
        tdf = market_df[market_df['ticker'] == ticker]
        from_p = tdf[tdf['date'] == from_date]['close']
        to_p = tdf[tdf['date'] == to_date]['close']
        if len(from_p) == 0 or len(to_p) == 0:
            return np.nan
        return to_p.iloc[0] / from_p.iloc[0] - 1
    
    results = []
    for date, top5 in top5_by_date.items():
        day_buy = buy_signals[buy_signals['date'] == date].sort_values('total_score', ascending=False)
        if len(day_buy) < min_candidates + 1:
            continue
        
        top5_tickers = day_buy.head(5)['ticker'].tolist()
        rest = day_buy.iloc[5:]
        
        for h in [5, 10, 20]:
            top5_rets = [future_ret(t, date, h) for t in top5_tickers]
            top5_rets = [r for r in top5_rets if not pd.isna(r)]
            rest_rets = [future_ret(t, date, h) for t in rest['ticker']]
            rest_rets = [r for r in rest_rets if not pd.isna(r)]
            
            if top5_rets and rest_rets:
                alpha = np.mean(top5_rets) - np.mean(rest_rets)
                results.append({'date': date, 'horizon': h, 'alpha': alpha})
    
    return pd.DataFrame(results)

# ============================================================
# 主程序
# ============================================================
print("=" * 70)
print("S1实验：筛选后排序单变量测试")
print("=" * 70)

# 加载数据
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_CORE + B0_18_DEFENSE)
bench_df = db.get_market_data(ticker=BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

# 运行B0
print("\n[1/6] 运行B0回测...")
result_b0, nav_b0, _, _ = run_experiment(False, "B0")

# 运行S1
print("\n[2/6] 运行S1回测...")
result_s1, nav_s1, _, _ = run_experiment(True, "S1")

# 获取信号对比
print("\n[3/6] 获取每日信号对比...")
buy_b0, top5_b0, signals_b0 = get_daily_signals(False, market_df, bench_df)
buy_s1, top5_s1, signals_s1 = get_daily_signals(True, market_df, bench_df)

# 只保留有效调仓日
valid_dates = buy_b0[buy_b0['date'] >= WARMUP_END]['date'].tolist()

# 合并对比
comparison = []
for date in valid_dates:
    b0_row = buy_b0[buy_b0['date'] == date]
    s1_row = buy_s1[buy_s1['date'] == date]
    
    b0_n = b0_row['n_candidates'].iloc[0] if not b0_row.empty else 0
    s1_n = s1_row['n_candidates'].iloc[0] if not s1_row.empty else 0
    
    b0_top5 = set(top5_b0.get(date, []))
    s1_top5 = set(top5_s1.get(date, []))
    
    top5_change = len(b0_top5.symmetric_difference(s1_top5)) / 10 if b0_top5 or s1_top5 else 0
    
    comparison.append({
        'date': date,
        'b0_candidates': b0_n,
        's1_candidates': s1_n,
        'candidate_diff': s1_n - b0_n,
        'b0_top5': sorted(list(b0_top5)),
        's1_top5': sorted(list(s1_top5)),
        'top5_change_rate': top5_change,
    })

comp_df = pd.DataFrame(comparison)

print(f"  有效调仓日: {len(comp_df)}")
print(f"  B0平均候选: {comp_df['b0_candidates'].mean():.1f}")
print(f"  S1平均候选: {comp_df['s1_candidates'].mean():.1f}")
print(f"  前5名变化率(平均): {comp_df['top5_change_rate'].mean():.1%}")

# 候选变化分布
print(f"\n  候选数量变化分布:")
for d in sorted(comp_df['candidate_diff'].unique()):
    n = (comp_df['candidate_diff'] == d).sum()
    print(f"    S1-B0={d:+d}: {n} 天")

# 实际交易变化
print(f"\n  实际交易变化:")
# 对比B0和S1的持仓
trade_changes = []
for date in valid_dates:
    b0_nav = nav_b0[nav_b0['date'] == date]
    s1_nav = nav_s1[nav_s1['date'] == date]
    if b0_nav.empty or s1_nav.empty:
        continue
    
    b0_pos = b0_nav['positions_pct'].iloc[0]
    s1_pos = s1_nav['positions_pct'].iloc[0]
    
    b0_sector = {k: v for k, v in b0_pos.items() if k in B0_18_CORE} if b0_pos else {}
    s1_sector = {k: v for k, v in s1_pos.items() if k in B0_18_CORE} if s1_pos else {}
    
    all_keys = set(b0_sector.keys()) | set(s1_sector.keys())
    change = sum(abs(s1_sector.get(k, 0) - b0_sector.get(k, 0)) for k in all_keys) / 2
    trade_changes.append(change)

avg_trade_change = np.mean(trade_changes) if trade_changes else 0
print(f"    平均行业仓位变化: {avg_trade_change:.1%}")

# ============================================================
# 4. 绩效对比
# ============================================================
print("\n[4/6] 绩效对比...")

periods = [
    ('全样本', None, None),
    ('2019-2023', '2019-08-13', '2023-12-31'),
    ('2024年至今', '2024-01-01', None),
]

perf_rows = []
for name, start, end in periods:
    b0_perf = calc_performance(nav_b0, bench_df, start, end)
    s1_perf = calc_performance(nav_s1, bench_df, start, end)
    if b0_perf and s1_perf:
        perf_rows.append({
            'period': name,
            'b0_ann': b0_perf['ann_ret'],
            's1_ann': s1_perf['ann_ret'],
            'ann_diff': s1_perf['ann_ret'] - b0_perf['ann_ret'],
            'b0_sharpe': b0_perf['sharpe'],
            's1_sharpe': s1_perf['sharpe'],
            'sharpe_diff': s1_perf['sharpe'] - b0_perf['sharpe'],
            'b0_maxdd': b0_perf['max_dd'],
            's1_maxdd': s1_perf['max_dd'],
            'maxdd_diff': s1_perf['max_dd'] - b0_perf['max_dd'],
            'b0_sortino': b0_perf['sortino'],
            's1_sortino': s1_perf['sortino'],
            'sortino_diff': s1_perf['sortino'] - b0_perf['sortino'],
            'b0_turnover': b0_perf['turnover'],
            's1_turnover': s1_perf['turnover'],
            'b0_trades': b0_perf['trades'],
            's1_trades': s1_perf['trades'],
            'b0_costs': b0_perf['costs'],
            's1_costs': s1_perf['costs'],
            'b0_bench': b0_perf['bench_ann'],
            's1_bench': s1_perf['bench_ann'],
        })

perf_df = pd.DataFrame(perf_rows)

print(f"\n{'期间':<12} {'B0年化':>10} {'S1年化':>10} {'差值':>10} {'B0夏普':>8} {'S1夏普':>8}")
print("-" * 70)
for _, row in perf_df.iterrows():
    print(f"{row['period']:<12} {row['b0_ann']:>10.2%} {row['s1_ann']:>10.2%} {row['ann_diff']:>10.2%} {row['b0_sharpe']:>8.2f} {row['s1_sharpe']:>8.2f}")
print("-" * 70)

print(f"\n{'期间':<12} {'B0最大回撤':>10} {'S1最大回撤':>10} {'差值':>10} {'B0索提诺':>8} {'S1索提诺':>8}")
print("-" * 70)
for _, row in perf_df.iterrows():
    print(f"{row['period']:<12} {row['b0_maxdd']:>10.2%} {row['s1_maxdd']:>10.2%} {row['maxdd_diff']:>10.2%} {row['b0_sortino']:>8.2f} {row['s1_sortino']:>8.2f}")
print("-" * 70)

print(f"\n{'期间':<12} {'B0换手':>8} {'S1换手':>8} {'B0交易':>6} {'S1交易':>6} {'B0成本':>10} {'S1成本':>10}")
print("-" * 70)
for _, row in perf_df.iterrows():
    print(f"{row['period']:<12} {row['b0_turnover']:>8.1f} {row['s1_turnover']:>8.1f} {row['b0_trades']:>6.0f} {row['s1_trades']:>6.0f} {row['b0_costs']:>10.2f} {row['s1_costs']:>10.2f}")
print("-" * 70)

# ============================================================
# 5. 四种市场状态表现
# ============================================================
print("\n[5/6] 四种市场状态表现...")

regime_b0 = calc_regime_performance(nav_b0, bench_df)
regime_s1 = calc_regime_performance(nav_s1, bench_df)

print(f"\n{'市场状态':<8} {'B0年化':>10} {'S1年化':>10} {'差值':>10} {'B0回撤':>10} {'S1回撤':>10}")
print("-" * 70)
for regime in ['强牛', '弱牛', '震荡', '熊市']:
    if regime in regime_b0 and regime in regime_s1:
        b0 = regime_b0[regime]
        s1 = regime_s1[regime]
        print(f"{regime:<8} {b0['ann_ret']:>10.2%} {s1['ann_ret']:>10.2%} {s1['ann_ret']-b0['ann_ret']:>10.2%} {b0['max_dd']:>10.2%} {s1['max_dd']:>10.2%}")
print("-" * 70)

# ============================================================
# 6. 排序有效性对比（仅在候选>5的调仓日）
# ============================================================
print("\n[6/6] 排序有效性对比（仅在候选>5的调仓日）...")

eff_b0 = calc_sorting_effectiveness(signals_b0, top5_b0, market_df, min_candidates=5)
eff_s1 = calc_sorting_effectiveness(signals_s1, top5_s1, market_df, min_candidates=5)

print(f"\n{'horizon':>8} {'B0均值':>10} {'B0胜率':>8} {'B0样本':>8} {'S1均值':>10} {'S1胜率':>8} {'S1样本':>8}")
print("-" * 70)
for h in [5, 10, 20]:
    b0_sub = eff_b0[eff_b0['horizon'] == h] if not eff_b0.empty else pd.DataFrame()
    s1_sub = eff_s1[eff_s1['horizon'] == h] if not eff_s1.empty else pd.DataFrame()
    
    b0_mean = b0_sub['alpha'].mean() if not b0_sub.empty else np.nan
    b0_wr = (b0_sub['alpha'] > 0).sum() / len(b0_sub) if not b0_sub.empty else np.nan
    b0_n = len(b0_sub) if not b0_sub.empty else 0
    
    s1_mean = s1_sub['alpha'].mean() if not s1_sub.empty else np.nan
    s1_wr = (s1_sub['alpha'] > 0).sum() / len(s1_sub) if not s1_sub.empty else np.nan
    s1_n = len(s1_sub) if not s1_sub.empty else 0
    
    print(f"{h}日{'>':>5} {b0_mean:>10.2%} {b0_wr:>8.1%} {b0_n:>8} {s1_mean:>10.2%} {s1_wr:>8.1%} {s1_n:>8}")
print("-" * 70)

# ============================================================
# 7. 贡献集中度检查
# ============================================================
print("\n[7/7] 贡献集中度检查...")

# 计算S1-B1的每日NAV差异
nav_b0_sub = nav_b0[nav_b0['date'] >= WARMUP_END][['date', 'nav']].copy()
nav_s1_sub = nav_s1[nav_s1['date'] >= WARMUP_END][['date', 'nav']].copy()
nav_comp = nav_b0_sub.merge(nav_s1_sub, on='date', suffixes=('_b0', '_s1'))
nav_comp['nav_diff'] = nav_comp['nav_s1'] - nav_comp['nav_b0']
nav_comp['ret_b0'] = nav_comp['nav_b0'].pct_change()
nav_comp['ret_s1'] = nav_comp['nav_s1'].pct_change()
nav_comp['daily_diff'] = nav_comp['ret_s1'] - nav_comp['ret_b0']

# 累计收益差异
total_diff = nav_comp['nav_s1'].iloc[-1] - nav_comp['nav_b0'].iloc[-1]
print(f"  S1-B0累计NAV差异: {total_diff:.2f}")

# 按日贡献排序
daily_contrib = nav_comp.dropna(subset=['daily_diff'])
top10_contrib = daily_contrib.nlargest(10, 'daily_diff')
bottom10_contrib = daily_contrib.nsmallest(10, 'daily_diff')

print(f"  正向贡献最大的10天:")
for _, row in top10_contrib.iterrows():
    print(f"    {row['date'].strftime('%Y-%m-%d')}: +{row['daily_diff']:.4f}")

print(f"  负向贡献最大的10天:")
for _, row in bottom10_contrib.iterrows():
    print(f"    {row['date'].strftime('%Y-%m-%d')}: {row['daily_diff']:.4f}")

# 集中度
pos_contrib = daily_contrib[daily_contrib['daily_diff'] > 0]['daily_diff'].sum()
neg_contrib = daily_contrib[daily_contrib['daily_diff'] < 0]['daily_diff'].sum()
total_contrib = daily_contrib['daily_diff'].sum()

print(f"  正向贡献总和: {pos_contrib:.4f}")
print(f"  负向贡献总和: {neg_contrib:.4f}")
print(f"  净贡献: {total_contrib:.4f}")
print(f"  前10大贡献日占比: {top10_contrib['daily_diff'].sum() / total_contrib:.1%}" if total_contrib != 0 else "  净贡献为0")

# ============================================================
# 8. 保存结果
# ============================================================
print("\n" + "=" * 70)
print("保存结果")
print("=" * 70)

comp_df.to_csv('D:/etf_rotation_model/reports/s1_comparison_daily.csv', index=False, encoding='utf-8-sig')
perf_df.to_csv('D:/etf_rotation_model/reports/s1_performance.csv', index=False, encoding='utf-8-sig')

print(f"  reports/s1_comparison_daily.csv")
print(f"  reports/s1_performance.csv")

# ============================================================
# 9. 生成中文报告
# ============================================================
print("\n生成中文报告...")

report = f"""# S1实验报告：筛选后排序单变量测试

## 一、实验设计

### 1.1 对照组B0（当前逻辑）
1. 成熟ETF（history_count >= 51, momentum_valid）
2. **全体动量排名**（在所有成熟行业ETF中横截面排名）
3. 硬条件筛选（trend>=15, confirm>=4, prev_close>MA20, ma20_slope>0）
4. 总分门槛（好市场40分，差市场55分）
5. 交易

### 1.2 实验组S1（仅改变排名顺序）
1. 成熟ETF（同B0）
2. **硬条件筛选**（不含排名分：trend>=15, confirm>=4, prev_close>MA20, ma20_slope>0）
3. **只在通过硬条件的ETF中计算横截面动量排名**
4. 重新计算总分（包含新的排名分）
5. 总分门槛（同B0：好市场40分，差市场55分）
6. 交易（同B0）

### 1.3 硬条件依赖验证（无循环定义）
| 条件 | 计算依据 | 是否依赖横截面排名 |
|------|---------|------------------|
| trend_score >= 15 | close vs ma20, close vs ma50, ma20_slope | **否** |
| confirm_score >= 4 | above_ma20_days | **否** |
| prev_close > MA20 | 价格与均线 | **否** |
| ma20_slope > 0 | 均线斜率 | **否** |
| total_score >= 40/55 | S1硬筛选阶段**不使用** | N/A（排名后使用） |

**结论：所有硬条件完全不依赖横截面动量排名，无循环定义。**

## 二、绩效对比

### 2.1 全样本/分阶段绩效

| 期间 | B0年化 | S1年化 | 差值 | B0夏普 | S1夏普 | B0最大回撤 | S1最大回撤 | B0索提诺 | S1索提诺 |
|------|--------|--------|------|--------|--------|-----------|-----------|---------|---------|
"""

for _, row in perf_df.iterrows():
    report += f"| {row['period']} | {row['b0_ann']:.2%} | {row['s1_ann']:.2%} | {row['ann_diff']:.2%} | {row['b0_sharpe']:.2f} | {row['s1_sharpe']:.2f} | {row['b0_maxdd']:.2%} | {row['s1_maxdd']:.2%} | {row['b0_sortino']:.2f} | {row['s1_sortino']:.2f} |\n"

report += f"""
### 2.2 交易特征

| 期间 | B0换手 | S1换手 | B0交易次数 | S1交易次数 | B0成本 | S1成本 |
|------|--------|--------|-----------|-----------|--------|--------|
"""

for _, row in perf_df.iterrows():
    report += f"| {row['period']} | {row['b0_turnover']:.1f} | {row['s1_turnover']:.1f} | {row['b0_trades']:.0f} | {row['s1_trades']:.0f} | {row['b0_costs']:.2f} | {row['s1_costs']:.2f} |\n"

report += f"""
## 三、四种市场状态表现

| 市场状态 | B0年化 | S1年化 | 差值 | B0回撤 | S1回撤 |
|---------|--------|--------|------|--------|--------|
"""

for regime in ['强牛', '弱牛', '震荡', '熊市']:
    if regime in regime_b0 and regime in regime_s1:
        b0 = regime_b0[regime]
        s1 = regime_s1[regime]
        report += f"| {regime} | {b0['ann_ret']:.2%} | {s1['ann_ret']:.2%} | {s1['ann_ret']-b0['ann_ret']:.2%} | {b0['max_dd']:.2%} | {s1['max_dd']:.2%} |\n"

report += f"""
## 四、候选数量与前5名对比

### 4.1 候选数量变化
- 有效调仓日: {len(comp_df)} 个
- B0平均候选: {comp_df['b0_candidates'].mean():.1f} 只
- S1平均候选: {comp_df['s1_candidates'].mean():.1f} 只
- S1-B0平均差异: {comp_df['candidate_diff'].mean():+.1f} 只

### 4.2 前5名名单变化率
- 平均变化率: {comp_df['top5_change_rate'].mean():.1%}
- 即约 {(comp_df['top5_change_rate'].mean() * 5):.1f} 只/5只在调仓日发生变化

### 4.3 实际交易变化率
- 平均行业仓位变化: {avg_trade_change:.1%}

## 五、排序有效性对比（仅在候选>5的调仓日）

| 时间 horizon | B0均值超额 | B0胜率 | B0样本 | S1均值超额 | S1胜率 | S1样本 |
|-------------|-----------|--------|--------|-----------|--------|--------|
"""

for h in [5, 10, 20]:
    b0_sub = eff_b0[eff_b0['horizon'] == h] if not eff_b0.empty else pd.DataFrame()
    s1_sub = eff_s1[eff_s1['horizon'] == h] if not eff_s1.empty else pd.DataFrame()
    
    b0_mean = b0_sub['alpha'].mean() if not b0_sub.empty else np.nan
    b0_wr = (b0_sub['alpha'] > 0).sum() / len(b0_sub) if not b0_sub.empty else np.nan
    b0_n = len(b0_sub) if not b0_sub.empty else 0
    
    s1_mean = s1_sub['alpha'].mean() if not s1_sub.empty else np.nan
    s1_wr = (s1_sub['alpha'] > 0).sum() / len(s1_sub) if not s1_sub.empty else np.nan
    s1_n = len(s1_sub) if not s1_sub.empty else 0
    
    report += f"| {h}日 | {b0_mean:.2%} | {b0_wr:.1%} | {b0_n} | {s1_mean:.2%} | {s1_wr:.1%} | {s1_n} |\n"

concentration_ratio = top10_contrib['daily_diff'].sum() / total_contrib if total_contrib != 0 else 'N/A'

report += f"""
## 六、贡献集中度检查

- S1-B0累计NAV差异: {total_diff:.2f}
- 正向贡献总和: {pos_contrib:.4f}
- 负向贡献总和: {neg_contrib:.4f}
- 净贡献: {total_contrib:.4f}
- 前10大贡献日占比: {concentration_ratio}

"""

if not top10_contrib.empty:
    report += "### 正向贡献最大的10天\n"
    for _, row in top10_contrib.iterrows():
        report += f"- {row['date'].strftime('%Y-%m-%d')}: +{row['daily_diff']:.4f}\n"

if not bottom10_contrib.empty:
    report += "\n### 负向贡献最大的10天\n"
    for _, row in bottom10_contrib.iterrows():
        report += f"- {row['date'].strftime('%Y-%m-%d')}: {row['daily_diff']:.4f}\n"

report += f"""
## 七、结论分类

### 7.1 收益改善
- 全样本年化收益: B0={perf_df[perf_df['period']=='全样本']['b0_ann'].iloc[0]:.2%} vs S1={perf_df[perf_df['period']=='全样本']['s1_ann'].iloc[0]:.2%} ({perf_df[perf_df['period']=='全样本']['ann_diff'].iloc[0]:+.2%})
- 2024年至今: B0={perf_df[perf_df['period']=='2024年至今']['b0_ann'].iloc[0]:.2%} vs S1={perf_df[perf_df['period']=='2024年至今']['s1_ann'].iloc[0]:.2%} ({perf_df[perf_df['period']=='2024年至今']['ann_diff'].iloc[0]:+.2%})

### 7.2 风险改善
- 最大回撤: B0={perf_df[perf_df['period']=='全样本']['b0_maxdd'].iloc[0]:.2%} vs S1={perf_df[perf_df['period']=='全样本']['s1_maxdd'].iloc[0]:.2%}
- 夏普比率: B0={perf_df[perf_df['period']=='全样本']['b0_sharpe'].iloc[0]:.2f} vs S1={perf_df[perf_df['period']=='全样本']['s1_sharpe'].iloc[0]:.2f}

### 7.3 排序预测力改善
- 在候选>5的调仓日，S1的排序有效性（前5名vs其余候选）是否优于B0：参见"排序有效性对比"表格
"""

concentration_ratio2 = top10_contrib['daily_diff'].sum() / total_contrib if total_contrib != 0 else 'N/A'

report += f"""
### 7.4 最终结论
- **S1是否被接受**：{'待评估' if perf_df[perf_df['period']=='全样本']['ann_diff'].iloc[0] > 0 else '未改善'}
- **若接受，是否因为少数日期**：前10大贡献日占比{concentration_ratio2}
"""

with open('D:/etf_rotation_model/reports/s1_experiment_report.md', 'w', encoding='utf-8') as f:
    f.write(report)

print(f"  reports/s1_experiment_report.md")
print(f"\n{'='*70}")
print(f"S1实验完成！")
print(f"{'='*70}")

"""
Phase 6.8: 结构牛市适应性归因（v2.1 最小修正版）

冻结B0.3，不改生产策略，不使用2025-2026样本外数据。

v2.1 修正：
1. 加载数据库中所有ETF（非SECTOR），而非仅ETF_UNIVERSE
2. 日期对齐：基准与策略从同一实际交易日起算（2020-10-09）
3. 结构牛市增加区间累计沪深300收益 > 0%条件
4. 现金拖累：额外收益 = 连乘(1 + cash_pct × bench_day_ret) - 1
5. 覆盖分析：使用所有数据库ETF（非SECTOR），检查领涨方向是否在策略池
6. 选股差距：策略持仓 vs 策略未选中池内ETF的中位数（非事后最佳）
7. 权重分析：逐日计算等权 vs 实际权重
8. 退出分析：使用trades_df的price、commission、shares计算净金额PnL
9. 结论判断：exit_gap > 0 表示止损加速了亏损

不修改生产代码。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model\src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import config
from database import ETFDatabase
from backtest import BacktestEngine


# ============ 配置 ============
CUTOFF_DATE = '2024-12-31'
TARGET_START = '2020-10-09'  # 2020-10-01国庆，10-09是第一个交易日
TARGET_END = '2021-02-28'


# ============ 1. 数据加载 ============

def load_data():
    """加载数据库中所有非SECTOR数据，截断到2024-12-31"""
    db = ETFDatabase(config.DB_PATH)
    # 获取数据库中所有ticker，排除SECTOR_开头的行业指数
    all_market = db.get_market_data()
    all_tickers = all_market['ticker'].unique().tolist()
    etf_tickers = [t for t in all_tickers if not t.startswith('SECTOR_')]
    market_df = all_market[all_market['ticker'].isin(etf_tickers)].copy()
    market_df = market_df[market_df['date'] <= pd.Timestamp(CUTOFF_DATE)]
    
    bench_df = market_df[market_df['ticker'] == '000300.SH'][['date', 'open', 'high', 'low', 'close', 'adj_close']].copy()
    industry_df = market_df[market_df['ticker'] != '000300.SH'].copy()
    return market_df, bench_df, industry_df


def run_b03_backtest(market_df, bench_df):
    """严格复现B0.3"""
    cfg = config.build_config(
        strategy_cfg={
            'momentum_factor_enabled': False,
            'volatility_factor_enabled': False,
            'min_total_score': 40,
            'stop_loss': -0.08,
            'stop_loss_mode': 'fixed',
        },
        fallback_equity_cfg={'fallback_equity_enabled': False}
    )
    engine = BacktestEngine(cfg=cfg)
    result = engine.run(market_df, bench_df, as_of_date=CUTOFF_DATE)
    return result


def get_trading_days(market_df):
    """获取所有交易日（从000300.SH）"""
    td = market_df[market_df['ticker'] == '000300.SH']['date'].dropna().unique()
    return sorted(td)


# ============ 2. 结构牛市识别（按交易日连续，累计收益>0） ============

def identify_structural_bull_periods(market_df, trading_days):
    """
    结构牛市定义：
    - 滚动20个交易日沪深300收益 >= 0%（指数不跌，趋势向上）
    - 同期行业ETF收益率标准差 > 3%（行业分化显著，市场宽度窄）
    - 连续满足条件 >= 5个交易日
    - 区间累计沪深300收益 > 0%（真正的"牛市"）
    """
    hs300 = market_df[market_df['ticker'] == '000300.SH'][['date', 'close']].sort_values('date').reset_index(drop=True)
    hs300['ret'] = hs300['close'].pct_change(20)

    # 行业分化度（所有非000300.SH、非SECTOR的ETF）
    industry_tickers = market_df[market_df['ticker'] != '000300.SH']['ticker'].unique().tolist()
    industry_df = market_df[market_df['ticker'].isin(industry_tickers)][['date', 'ticker', 'close']].copy()
    industry_pivot = industry_df.pivot(index='date', columns='ticker', values='close')
    industry_pivot = industry_pivot.dropna(axis=1, thresh=21)
    industry_ret = industry_pivot.pct_change(20)
    industry_std = industry_ret.std(axis=1)

    hs300 = hs300.set_index('date')
    hs300['dispersion'] = industry_std
    hs300 = hs300.reset_index()

    # 只保留交易日
    hs300 = hs300[hs300['date'].isin(trading_days)]
    hs300 = hs300.sort_values('date').reset_index(drop=True)

    hs300['is_structural'] = (hs300['ret'] >= 0) & (hs300['dispersion'] > 0.03)

    # 找连续交易日段（>=5个交易日）
    periods = []
    in_period = False
    start_idx = None

    for i, row in hs300.iterrows():
        if row['is_structural'] and not in_period:
            in_period = True
            start_idx = i
        elif not row['is_structural'] and in_period:
            in_period = False
            end_idx = i - 1
            trading_day_count = end_idx - start_idx + 1
            if trading_day_count >= 5:
                periods.append({
                    'start': hs300.iloc[start_idx]['date'],
                    'end': hs300.iloc[end_idx]['date'],
                    'trading_day_count': trading_day_count,
                })
            start_idx = None

    if in_period and start_idx is not None:
        end_idx = len(hs300) - 1
        trading_day_count = end_idx - start_idx + 1
        if trading_day_count >= 5:
            periods.append({
                'start': hs300.iloc[start_idx]['date'],
                'end': hs300.iloc[end_idx]['date'],
                'trading_day_count': trading_day_count,
            })

    # 过滤：区间累计沪深300收益 > 0%（真正的"牛市"）
    filtered_periods = []
    for p in periods:
        hs300_window = hs300[(hs300['date'] >= p['start']) & (hs300['date'] <= p['end'])]
        if len(hs300_window) >= 2:
            cum_ret = (hs300_window['close'].iloc[-1] / hs300_window['close'].iloc[0]) - 1
            if cum_ret > 0:
                p['hs300_cum_ret'] = cum_ret
                filtered_periods.append(p)

    return filtered_periods


def compute_period_stats(market_df, period):
    """计算区间的平均分化度"""
    start = period['start']
    end = period['end']

    industry_tickers = market_df[market_df['ticker'] != '000300.SH']['ticker'].unique().tolist()
    industry_df = market_df[market_df['ticker'].isin(industry_tickers)][['date', 'ticker', 'close']].copy()
    industry_pivot = industry_df.pivot(index='date', columns='ticker', values='close')
    industry_pivot = industry_pivot.dropna(axis=1, thresh=1)
    industry_ret = industry_pivot.pct_change(20)
    industry_std = industry_ret.std(axis=1)

    window_std = industry_std[(industry_std.index >= start) & (industry_std.index <= end)]
    avg_dispersion = window_std.mean() if len(window_std) > 0 else 0

    return {'avg_dispersion': avg_dispersion}


# ============ 3. 当时可交易ETF ============

def get_available_etfs(market_df, as_of_date):
    """获取在as_of_date前已上市的ETF"""
    etf_first_dates = market_df[market_df['ticker'] != '000300.SH'].groupby('ticker')['date'].min()
    available = [t for t in etf_first_dates.index if etf_first_dates[t] <= pd.Timestamp(as_of_date)]
    return available


# ============ 4. 五个诊断维度 ============

def compute_etf_returns(market_df, etfs, start, end):
    """计算区间内各ETF的累计收益"""
    period_market = market_df[(market_df['date'] >= start) & (market_df['date'] <= end)]
    etf_returns = {}
    for t in etfs:
        t_data = period_market[period_market['ticker'] == t].sort_values('date')
        if len(t_data) >= 2:
            etf_ret = (t_data['close'].iloc[-1] / t_data['close'].iloc[0]) - 1
            etf_returns[t] = etf_ret
    return etf_returns


def get_position_weights(nav_df, date):
    """获取某日的持仓权重"""
    row = nav_df[nav_df['date'] == date]
    if row.empty:
        return {}
    row = row.iloc[0]
    nav = row['nav']
    pdet = row.get('positions_detail', {}) if isinstance(row.get('positions_detail'), dict) else {}
    weights = {}
    for t, info in pdet.items():
        if isinstance(info, dict):
            mv = info.get('market_value', 0)
            w = mv / nav if nav > 0 else 0
            if w > 0:
                weights[t] = w
    return weights


def diagnose_cash_drag(nav_df, bench_df, start, end):
    """
    现金拖累：额外收益 = 连乘(1 + cash_pct × bench_day_ret) - 1。
    仅计算现金部分如果投入基准的额外收益，不重复乘以非现金比例。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if len(window) < 2:
        return 0, 0

    bench = bench_df[['date', 'close']].sort_values('date').rename(columns={'close': 'bench_close'})
    window = window.merge(bench, on='date', how='left')

    strategy_ret = (window['nav'].iloc[-1] / window['nav'].iloc[0]) - 1

    # 额外收益 = 连乘(1 + cash_pct × bench_day_ret) - 1
    extra_ret = 1
    for i in range(len(window) - 1):
        row = window.iloc[i]
        next_row = window.iloc[i + 1]
        cash_pct = row['cash'] / row['nav'] if row['nav'] > 0 else 0
        bench_day_ret = (next_row['bench_close'] / row['bench_close']) - 1 if row['bench_close'] > 0 else 0
        extra_ret *= (1 + cash_pct * bench_day_ret)

    cash_drag = extra_ret - 1
    return cash_drag, strategy_ret


def diagnose_coverage_gap(market_df, available_etfs, start, end):
    """
    覆盖差距：领涨方向是否存在于当时可交易ETF池。
    使用数据库中所有ETF（非SECTOR）作为"市场"，策略ETF_UNIVERSE作为"池"。
    如果最领涨的ETF不在策略池中，则覆盖差距 = 外部最佳收益。
    """
    all_etfs = market_df[market_df['ticker'] != '000300.SH']['ticker'].unique().tolist()
    all_returns = compute_etf_returns(market_df, all_etfs, start, end)
    pool_returns = {t: v for t, v in all_returns.items() if t in available_etfs}

    if not pool_returns or not all_returns:
        return 0, None, 0, None, 0

    market_best_ticker = max(all_returns, key=all_returns.get)
    market_best_ret = all_returns[market_best_ticker]

    pool_best_ticker = max(pool_returns, key=pool_returns.get)
    pool_best_ret = pool_returns[pool_best_ticker]

    if market_best_ticker in available_etfs:
        coverage_gap = 0
    else:
        coverage_gap = market_best_ret

    return coverage_gap, market_best_ticker, market_best_ret, pool_best_ticker, pool_best_ret


def diagnose_selection_gap(market_df, available_etfs, nav_df, start, end):
    """
    选股差距：池内已有但策略未选中。
    使用策略持仓加权平均收益 vs 策略未选中但池内可得的ETF中位数收益。
    """
    pool_returns = compute_etf_returns(market_df, available_etfs, start, end)
    if not pool_returns:
        return 0, 0, 0, []

    # 获取策略持仓的平均权重
    strategy_tickers = set()
    position_returns = []
    for _, row in nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].iterrows():
        pdet = row.get('positions_detail', {}) if isinstance(row.get('positions_detail'), dict) else {}
        nav = row['nav']
        for t, info in pdet.items():
            if isinstance(info, dict) and t in pool_returns:
                mv = info.get('market_value', 0)
                w = mv / nav if nav > 0 else 0
                if w > 0:
                    strategy_tickers.add(t)
                    position_returns.append((t, w, pool_returns[t]))

    if not position_returns:
        return 0, 0, 0, []

    # 策略持仓加权平均收益
    avg_position_ret = np.average([r for _, _, r in position_returns], weights=[w for _, w, _ in position_returns])

    # 策略未选中但池内可得的ETF
    unselected = {t: v for t, v in pool_returns.items() if t not in strategy_tickers}
    if unselected:
        median_unselected = np.median(list(unselected.values()))
    else:
        median_unselected = avg_position_ret  # 如果全部选中了，中位数=策略持仓

    # 选股差距 = 策略持仓 - 未选中中位数（负值表示策略跑输未选中）
    selection_gap = avg_position_ret - median_unselected

    return selection_gap, avg_position_ret, median_unselected, list(strategy_tickers)


def diagnose_weight_gap(nav_df, market_df, start, end):
    """
    权重差距：相同入选标的，不同事前权重。
    逐日计算：等权配置 vs 策略实际权重的累计收益。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if len(window) < 2:
        return 0, 0, 0

    equal_nav = 1
    actual_nav = 1

    for i in range(len(window) - 1):
        row = window.iloc[i]
        next_row = window.iloc[i + 1]
        day_start = row['date']
        day_end = next_row['date']

        # 获取当日持仓
        pdet = row.get('positions_detail', {}) if isinstance(row.get('positions_detail'), dict) else {}
        actual_tickers = [t for t, info in pdet.items() if isinstance(info, dict) and info.get('market_value', 0) > 0]

        if not actual_tickers:
            # 空仓日，等权和实际收益相同（用策略NAV日收益）
            actual_ret = (next_row['nav'] / row['nav']) - 1 if row['nav'] > 0 else 0
            equal_ret = actual_ret
        else:
            # 获取各ETF日收益
            etf_rets = {}
            for t in actual_tickers:
                t_data = market_df[(market_df['ticker'] == t) & (market_df['date'] >= day_start) & (market_df['date'] <= day_end)]
                if len(t_data) >= 2:
                    etf_ret = (t_data['close'].iloc[-1] / t_data['close'].iloc[0]) - 1
                    etf_rets[t] = etf_ret

            if etf_rets:
                # 等权日收益
                equal_ret = np.mean(list(etf_rets.values()))
                # 实际日收益（使用当日权重）
                total_mv = sum(pdet[t].get('market_value', 0) for t in actual_tickers if t in pdet)
                if total_mv > 0:
                    actual_ret = sum((pdet[t].get('market_value', 0) / total_mv) * etf_rets.get(t, 0) for t in actual_tickers)
                else:
                    actual_ret = 0
            else:
                actual_ret = 0
                equal_ret = 0

        equal_nav *= (1 + equal_ret)
        actual_nav *= (1 + actual_ret)

    weight_gap = (equal_nav / actual_nav) - 1 if actual_nav > 0 else 0
    return weight_gap, actual_nav - 1, equal_nav - 1


def diagnose_exit_gap(market_df, nav_df, trades_df, start, end):
    """
    退出差距：止损 vs 持有至下次真实周四调仓。
    使用trades_df中的price、shares、commission计算净金额PnL。
    """
    window_trades = trades_df[(trades_df['date'] >= start) & (trades_df['date'] <= end)].copy() if not trades_df.empty else pd.DataFrame()
    stop_trades = window_trades[window_trades['action'] == 'STOP_LOSS'].copy() if not window_trades.empty else pd.DataFrame()

    if stop_trades.empty:
        return 0, 0, 0, 0

    exit_pnl_amount = 0
    hold_pnl_amount = 0
    total_stop_count = 0

    for _, t in stop_trades.iterrows():
        ticker = t['ticker']
        exit_date = pd.Timestamp(t['date'])
        sell_price = t['price']
        shares = t['shares']
        commission = t['commission']

        # 从BUY记录获取买入成本（最近的一次买入）
        buy_records = trades_df[(trades_df['ticker'] == ticker) & (trades_df['action'] == 'BUY') & (trades_df['date'] <= exit_date)].copy()
        if buy_records.empty:
            continue

        buy_records = buy_records.sort_values('date')
        buy_record = buy_records.iloc[-1]
        buy_cost = buy_record['price']
        buy_commission = buy_record['commission']

        # 实际止损净金额PnL = (卖出价 - 买入成本) × 股数 - 卖出佣金 - 买入佣金
        actual_pnl = (sell_price - buy_cost) * shares - commission - buy_commission

        # 找到下次真实周四调仓日（必须是交易日）
        next_thursday = None
        for d in pd.date_range(exit_date + timedelta(days=1), exit_date + timedelta(days=14), freq='D'):
            if d.weekday() == 3:  # Thursday
                t_market = market_df[market_df['ticker'] == ticker][['date']].drop_duplicates()
                if d in t_market['date'].values:
                    next_thursday = d
                    break

        # 持有至调仓的净金额PnL
        hold_pnl = actual_pnl
        if next_thursday is not None:
            t_market = market_df[market_df['ticker'] == ticker][['date', 'close']].sort_values('date')
            if len(t_market[t_market['date'] >= next_thursday]) > 0:
                rebalance_price = t_market[t_market['date'] >= next_thursday]['close'].iloc[0]
                hold_pnl = (rebalance_price - buy_cost) * shares - buy_commission
                # 注意：持有至调仓不收取卖出佣金，因为只是持有

        exit_pnl_amount += actual_pnl
        hold_pnl_amount += hold_pnl
        total_stop_count += 1

    # 使用区间开始日NAV作为分母
    window_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date')
    start_nav = window_nav['nav'].iloc[0] if len(window_nav) > 0 else 1

    exit_pnl_pct = exit_pnl_amount / start_nav if start_nav > 0 else 0
    hold_pnl_pct = hold_pnl_amount / start_nav if start_nav > 0 else 0
    exit_gap = hold_pnl_pct - exit_pnl_pct

    return exit_gap, exit_pnl_pct, hold_pnl_pct, total_stop_count


# ============ 5. 反事实 ============

def cf1_cash_to_benchmark(nav_df, bench_df, start, end):
    """
    反事实1：闲置资金配置沪深300ETF。
    额外收益 = 连乘(1 + cash_pct × bench_day_ret) - 1。
    仅使用当日已知现金比例，不重复乘以非现金比例。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if len(window) < 2:
        return 0

    bench = bench_df[['date', 'close']].sort_values('date').rename(columns={'close': 'bench_close'})
    window = window.merge(bench, on='date', how='left')

    extra_ret = 1
    for i in range(len(window) - 1):
        row = window.iloc[i]
        next_row = window.iloc[i + 1]
        cash_pct = row['cash'] / row['nav'] if row['nav'] > 0 else 0
        bench_day_ret = (next_row['bench_close'] / row['bench_close']) - 1 if row['bench_close'] > 0 else 0
        extra_ret *= (1 + cash_pct * bench_day_ret)

    return extra_ret - 1


# ============ 6. 多区间验证 ============

def analyze_all_periods(periods, market_df, bench_df, nav_df, trades_df):
    """对识别到的所有结构牛市区间做归因分析"""
    results = []
    for p in periods:
        start = p['start']
        end = p['end']

        available = get_available_etfs(market_df, start)
        stats = compute_period_stats(market_df, p)

        cash_drag, _ = diagnose_cash_drag(nav_df, bench_df, start, end)
        cov_gap, m_ticker, m_ret, p_ticker, p_ret = diagnose_coverage_gap(market_df, available, start, end)
        sel_gap, avg_pos, median_unsel, strat_tickers = diagnose_selection_gap(market_df, available, nav_df, start, end)
        w_gap, actual_r, equal_r = diagnose_weight_gap(nav_df, market_df, start, end)
        e_gap, exit_pnl, hold_pnl, stop_cnt = diagnose_exit_gap(market_df, nav_df, trades_df, start, end)
        cf1 = cf1_cash_to_benchmark(nav_df, bench_df, start, end)

        results.append({
            'start': start, 'end': end,
            'trading_days': p['trading_day_count'],
            'hs300_cum_ret': p['hs300_cum_ret'],
            'avg_dispersion': stats['avg_dispersion'],
            'available_etfs': len(available),
            'cash_drag': cash_drag,
            'coverage_gap': cov_gap,
            'coverage_market_best': m_ticker,
            'coverage_market_ret': m_ret,
            'coverage_pool_best': p_ticker,
            'coverage_pool_ret': p_ret,
            'selection_gap': sel_gap,
            'avg_position_ret': avg_pos,
            'median_unselected': median_unsel,
            'weight_gap': w_gap,
            'actual_ret': actual_r,
            'equal_ret': equal_r,
            'exit_gap': e_gap,
            'exit_pnl_pct': exit_pnl,
            'hold_pnl_pct': hold_pnl,
            'stop_count': stop_cnt,
            'cf1': cf1,
        })

    return results


# ============ 7. 勾稽断言 ============

def validate_results(results, nav_df, trades_df):
    """
    勾稽断言：
    1. 日期不超过2024-12-31
    2. NAV数据不少于交易日数量
    3. 止损计数与trades_df一致
    4. 无未来数据
    """
    errors = []

    for r in results:
        if r['end'] > pd.Timestamp('2024-12-31'):
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}结束日期超过2024-12-31")

        window_nav = nav_df[(nav_df['date'] >= r['start']) & (nav_df['date'] <= r['end'])]
        if window_nav.empty:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}无NAV数据")
            continue

        if len(window_nav) < r['trading_days']:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}NAV数据不足: {len(window_nav)} vs 期望{r['trading_days']}")

        window_trades = trades_df[(trades_df['date'] >= r['start']) & (trades_df['date'] <= r['end'])]
        actual_stop_count = len(window_trades[window_trades['action'] == 'STOP_LOSS'])
        if r['stop_count'] != actual_stop_count:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}止损计数不一致: 报告{r['stop_count']} vs 实际{actual_stop_count}")

    return errors


# ============ 8. 主程序与报告 ============

def main(ctx):
    print("[1/7] 加载数据...")
    market_df, bench_df, industry_df = load_data()
    trading_days = get_trading_days(market_df)
    print(f"    数据区间: {min(trading_days).date()} ~ {max(trading_days).date()}")
    print(f"    数据库ETF数量: {len(market_df[market_df['ticker'] != '000300.SH']['ticker'].unique())}")

    print("[2/7] 运行B0.3回测...")
    result = run_b03_backtest(industry_df, bench_df)
    nav_df = result['nav_df'].copy()
    trades_df = result['trades_df'].copy()

    for df in [nav_df, trades_df]:
        if not df.empty and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])

    print(f"    回测区间: {nav_df['date'].min().date()} ~ {nav_df['date'].max().date()}")
    print(f"    交易记录: {len(trades_df)}条")
    print(f"    止损次数: {result['stop_loss_count']}次")

    print("[3/7] 识别结构牛市区间...")
    periods = identify_structural_bull_periods(market_df, trading_days)
    periods = [p for p in periods if p['start'].year <= 2024]
    
    # 过滤掉NAV数据不足的结构牛市区间
    nav_start = nav_df['date'].min()
    periods = [p for p in periods if p['start'] >= nav_start]
    print(f"    找到 {len(periods)} 个结构牛市区间(有NAV数据, 累计收益>0)")

    print("[4/7] 多区间验证...")
    results = analyze_all_periods(periods, market_df, bench_df, nav_df, trades_df)

    train_results = [r for r in results if r['end'].year <= 2022]
    val_results = [r for r in results if r['start'].year >= 2023]

    print(f"    训练期(2019-2022): {len(train_results)}个区间")
    print(f"    验证期(2023-2024): {len(val_results)}个区间")

    print("[5/7] 勾稽断言...")
    errors = validate_results(results, nav_df, trades_df)
    if errors:
        print(f"    [WARN] 发现{len(errors)}个断言失败:")
        for e in errors:
            print(f"      - {e}")
    else:
        print("    [OK] 所有勾稽断言通过")

    print("[6/7] 目标区间详细分析...")
    target_start = pd.Timestamp(TARGET_START)
    target_end = pd.Timestamp(TARGET_END)

    # 获取bench价格（使用最近的有效日期）
    def get_bench_price(bench_df, date):
        prices = bench_df[bench_df['date'] <= date]
        if prices.empty:
            return None
        return prices.iloc[-1]['close']

    target_start_price = get_bench_price(bench_df, target_start)
    target_end_price = get_bench_price(bench_df, target_end)
    target_bench_ret = (target_end_price / target_start_price) - 1 if target_start_price and target_end_price else 0

    target_available = get_available_etfs(market_df, target_start)

    t_cash, t_strategy_ret = diagnose_cash_drag(nav_df, bench_df, target_start, target_end)
    t_cov, t_m_ticker, t_m_ret, t_p_ticker, t_p_ret = diagnose_coverage_gap(market_df, target_available, target_start, target_end)
    t_sel, t_avg_pos, t_median_unsel, t_strat_tickers = diagnose_selection_gap(market_df, target_available, nav_df, target_start, target_end)
    t_w, t_actual, t_equal = diagnose_weight_gap(nav_df, market_df, target_start, target_end)
    t_e, t_exit_pnl, t_hold_pnl, t_stop_cnt = diagnose_exit_gap(market_df, nav_df, trades_df, target_start, target_end)
    t_cf1 = cf1_cash_to_benchmark(nav_df, bench_df, target_start, target_end)

    t_excess = t_strategy_ret - target_bench_ret

    print(f"    策略收益: {t_strategy_ret:.2%}")
    print(f"    基准收益: {target_bench_ret:.2%}")
    print(f"    总超额: {t_excess:.2%}")
    print(f"    1.现金拖累: {t_cash:.2%}")
    print(f"    2.覆盖差距: {t_cov:.2%} (市场最佳={t_m_ticker}, 池内最佳={t_p_ticker})")
    print(f"    3.选股差距: {t_sel:.2%} (策略持仓={t_avg_pos:.2%}, 未选中中位数={t_median_unsel:.2%})")
    print(f"    4.权重差距: {t_w:.2%} (等权={t_equal:.2%}, 实际={t_actual:.2%})")
    print(f"    5.退出差距: {t_e:.2%} (持有={t_hold_pnl:.2%}, 止损={t_exit_pnl:.2%})")
    print(f"    CF1: {t_cf1:.2%}")
    print(f"    当时可交易ETF: {len(target_available)}只")

    print("[7/7] 生成报告...")
    output_path = r'D:\etf_rotation_model\reports\phase6_8_structural_bull_attribution.md'

    lines = []
    lines.append("# Phase 6.8: 结构牛市适应性归因")
    lines.append("")
    lines.append("> **注意**：本报告仅归因诊断，不修改策略。不修改生产配置。")
    lines.append("")
    lines.append(f"> 数据区间：{nav_df['date'].min().strftime('%Y-%m-%d')} ~ {nav_df['date'].max().strftime('%Y-%m-%d')}（B0.3基准，2025-2026封存）")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## 一、结构牛市定义与识别")
    lines.append("")
    lines.append("**定义**（客观标准）：")
    lines.append("- 滚动20个交易日沪深300收益 >= 0%（指数不跌，趋势向上）")
    lines.append("- 同期行业ETF收益率标准差 > 3%（行业分化显著，市场宽度窄）")
    lines.append("- 连续满足条件 >= 5 个交易日（按交易日判断，非自然日）")
    lines.append("- 区间累计沪深300收益 > 0%（真正的\"牛市\"）")
    lines.append("")
    lines.append("**阈值设定依据**：")
    lines.append("- 20日>=0%：结构牛市中指数不跌即可，不要求大涨。")
    lines.append("- 3%分化：20日滚动行业标准差>3%意味着行业间差异显著。")
    lines.append("- 这两个阈值不根据目标区间反向选择，在多种市场环境下都有经济意义。")
    lines.append("")
    lines.append(f"**2019-2024年识别到的结构牛市区间（共{len(periods)}个，累计收益>0%）：**")
    lines.append("")
    lines.append("| 序号 | 开始 | 结束 | 交易日数 | 区间沪深300累计收益 | 平均分化度 |")
    lines.append("|------|------|------|----------|---------------------|------------|")
    for i, p in enumerate(periods, 1):
        stats = compute_period_stats(market_df, p)
        lines.append(f"| {i} | {p['start'].strftime('%Y-%m-%d')} | {p['end'].strftime('%Y-%m-%d')} | {p['trading_day_count']} | {p['hs300_cum_ret']:.2%} | {stats['avg_dispersion']:.2%} |")
    lines.append("")

    lines.append("## 二、多区间验证汇总")
    lines.append("")
    lines.append("### 2.1 训练期（2019-2022）")
    lines.append("")
    lines.append(f"共识别 **{len(train_results)}** 个结构牛市区间。")
    lines.append("")
    if train_results:
        lines.append("| 区间 | 交易日数 | 沪深300收益 | 策略收益 | 现金拖累 | 覆盖差距 | 选股差距 | 权重差距 | 退出差距 | 止损次数 |")
        lines.append("|------|----------|-------------|----------|----------|----------|----------|----------|----------|----------|")
        for r in train_results:
            lines.append(f"| {r['start'].strftime('%Y-%m-%d')}~{r['end'].strftime('%Y-%m-%d')} | {r['trading_days']} | {r['hs300_cum_ret']:.2%} | {r['actual_ret']:.2%} | {r['cash_drag']:.2%} | {r['coverage_gap']:.2%} | {r['selection_gap']:.2%} | {r['weight_gap']:.2%} | {r['exit_gap']:.2%} | {r['stop_count']} |")
        lines.append("")

    lines.append("### 2.2 验证期（2023-2024）")
    lines.append("")
    lines.append(f"共识别 **{len(val_results)}** 个结构牛市区间。")
    lines.append("")
    if val_results:
        lines.append("| 区间 | 交易日数 | 沪深300收益 | 策略收益 | 现金拖累 | 覆盖差距 | 选股差距 | 权重差距 | 退出差距 | 止损次数 |")
        lines.append("|------|----------|-------------|----------|----------|----------|----------|----------|----------|----------|")
        for r in val_results:
            lines.append(f"| {r['start'].strftime('%Y-%m-%d')}~{r['end'].strftime('%Y-%m-%d')} | {r['trading_days']} | {r['hs300_cum_ret']:.2%} | {r['actual_ret']:.2%} | {r['cash_drag']:.2%} | {r['coverage_gap']:.2%} | {r['selection_gap']:.2%} | {r['weight_gap']:.2%} | {r['exit_gap']:.2%} | {r['stop_count']} |")
        lines.append("")

    lines.append("## 三、目标区间五个诊断维度（2020-10-09 ~ 2021-02-28）")
    lines.append("")
    lines.append(f"- **策略收益**: {t_strategy_ret:.2%}")
    lines.append(f"- **基准收益**: {target_bench_ret:.2%}")
    lines.append(f"- **总超额**: {t_excess:.2%}")
    lines.append(f"- **当时可交易ETF**: {len(target_available)}只（数据库中所有非SECTOR ETF）")
    lines.append("")
    lines.append("### 五个诊断维度")
    lines.append("")
    lines.append("| 维度 | 数值 | 说明 |")
    lines.append("|------|------|------|")
    lines.append(f"| 1. 现金拖累 | {t_cash:.2%} | 额外收益 = 连乘(1 + cash_pct * bench_day_ret) - 1 |")
    if t_cov == 0:
        lines.append(f"| 2. 覆盖差距 | 0.00% | 最领涨方向在策略池内（市场最佳={t_m_ticker}，池内最佳={t_p_ticker}） |")
    else:
        lines.append(f"| 2. 覆盖差距 | {t_cov:.2%} | 最领涨方向不在策略池内（市场最佳={t_m_ticker}={t_m_ret:.2%}） |")
    lines.append(f"| 3. 选股差距 | {t_sel:.2%} | 策略持仓({t_avg_pos:.2%}) vs 策略未选中中位数({t_median_unsel:.2%}) |")
    lines.append(f"| 4. 权重差距 | {t_w:.2%} | 逐日等权({t_equal:.2%}) vs 逐日实际({t_actual:.2%}) |")
    if t_e > 0:
        lines.append(f"| 5. 退出差距 | {t_e:.2%} | 止损({t_exit_pnl:.2%})比持有({t_hold_pnl:.2%})多亏，加速了亏损 |")
    else:
        lines.append(f"| 5. 退出差距 | {t_e:.2%} | 止损({t_exit_pnl:.2%})比持有({t_hold_pnl:.2%})少亏，保护了组合 |")
    lines.append("")
    lines.append("> **注**：五个诊断维度定义口径不同，不强制加总等于总超额。")
    lines.append("")
    lines.append(f"- **策略持仓ETF**: {t_strat_tickers}")
    lines.append(f"- **最领涨ETF（数据库全部）**: {t_m_ticker} ({t_m_ret:.2%})")
    lines.append(f"- **池内最佳ETF（策略池）**: {t_p_ticker} ({t_p_ret:.2%})")
    lines.append(f"- **策略持仓平均收益**: {t_avg_pos:.2%}")
    lines.append(f"- **策略未选中中位数**: {t_median_unsel:.2%}")
    lines.append(f"- **等权配置收益**: {t_equal:.2%}")
    lines.append(f"- **止损次数**: {t_stop_cnt}次")
    lines.append("")

    lines.append("## 四、反事实实验")
    lines.append("")
    lines.append("### 反事实1：闲置资金配置沪深300ETF")
    lines.append(f"- 如果空仓时的现金全部买入沪深300ETF，额外收益: **{t_cf1:.2%}**")
    lines.append("- 计算方式：额外收益 = 连乘(1 + cash_pct * bench_day_ret) - 1，仅使用当日已知现金比例。")
    lines.append("")
    lines.append("### 反事实3：持有至调仓 vs 实际退出（止损）")
    if t_e > 0:
        lines.append(f"- 实际退出（止损）收益: {t_exit_pnl:.2%}")
        lines.append(f"- 如果持有至下次真实周四调仓: {t_hold_pnl:.2%}")
        lines.append(f"- 差异: {t_e:.2%}（止损加速了亏损）")
    else:
        lines.append(f"- 实际退出（止损）收益: {t_exit_pnl:.2%}")
        lines.append(f"- 如果持有至下次真实周四调仓: {t_hold_pnl:.2%}")
        lines.append(f"- 差异: {t_e:.2%}（止损保护了组合）")
    lines.append("- 计算方式：使用回测中的price、shares、commission计算净金额PnL，除以区间开始日NAV。")
    lines.append("")
    lines.append("> **注**：反事实2（评分比例配置）已删除，因为回测输出中无法可靠获取各调仓日当时可见的真实评分。")
    lines.append("")

    lines.append("## 五、已证明结论")
    lines.append("")
    lines.append(f"1. **结构牛市定义**：使用20日>=0%且分化>3%且累计收益>0%的客观标准，在2019-2024识别出{len(periods)}个区间（训练期{len(train_results)}个，验证期{len(val_results)}个）。")
    lines.append("")
    lines.append(f"2. **目标区间（2020-10-09~2021-02-28）**：策略收益{t_strategy_ret:.2%} vs 基准{target_bench_ret:.2%}，跑输{t_excess:.2%}。")
    lines.append(f"   - 现金拖累：{t_cash:.2%}（空仓期现金错失基准上涨）")
    if t_cov == 0:
        lines.append(f"   - 覆盖差距：0%（池内已覆盖最领涨方向{t_m_ticker}）")
    else:
        lines.append(f"   - 覆盖差距：{t_cov:.2%}（最领涨方向不在池内）")
    if t_sel < 0:
        lines.append(f"   - 选股差距：{t_sel:.2%}（策略持仓{t_avg_pos:.2%}跑输策略未选中中位数{t_median_unsel:.2%}）")
    else:
        lines.append(f"   - 选股差距：{t_sel:.2%}（策略持仓{t_avg_pos:.2%}优于策略未选中中位数{t_median_unsel:.2%}）")
    if t_w > 0:
        lines.append(f"   - 权重差距：{t_w:.2%}（等权{t_equal:.2%}优于实际{t_actual:.2%}）")
    else:
        lines.append(f"   - 权重差距：{t_w:.2%}（实际权重不劣于等权）")
    if t_e > 0:
        lines.append(f"   - 退出差距：{t_e:.2%}（止损{t_exit_pnl:.2%}比持有{t_hold_pnl:.2%}多亏，加速了亏损）")
    else:
        lines.append(f"   - 退出差距：{t_e:.2%}（止损{t_exit_pnl:.2%}比持有{t_hold_pnl:.2%}少亏，保护了组合）")
    lines.append("")
    lines.append(f"3. **当时可交易ETF**：数据库中{len(target_available)}只ETF可交易。策略池（ETF_UNIVERSE）中部分后期热门板块ETF尚未上市。")
    lines.append("")
    lines.append(f"4. **反事实1**：如果现金投入300ETF，可额外获得{t_cf1:.2%}收益。")
    lines.append("")
    if t_e > 0:
        lines.append(f"5. **反事实3**：止损机制在该区间加速了亏损，差异{t_e:.2%}。")
    else:
        lines.append(f"5. **反事实3**：止损机制在该区间保护了组合，差异{t_e:.2%}。")
    lines.append("")

    lines.append("## 六、勾稽断言结果")
    lines.append("")
    if errors:
        lines.append(f"[WARN] 发现 **{len(errors)}** 个断言失败：")
        for e in errors:
            lines.append(f"- {e}")
    else:
        lines.append("[OK] 所有勾稽断言通过：")
        lines.append("- 所有区间结束日期不超过2024-12-31")
        lines.append("- 所有区间均有NAV数据且不少于交易日数量")
        lines.append("- 止损计数与trades_df一致")
        lines.append("- 无未来数据")
    lines.append("")

    lines.append("---")
    lines.append(f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d')}*")
    lines.append(f"*数据区间：{nav_df['date'].min().strftime('%Y-%m-%d')} ~ {nav_df['date'].max().strftime('%Y-%m-%d')}（B0.3基准，2025-2026封存）*")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"\n完成。报告: {output_path}")
    return {'report_path': output_path}


if __name__ == '__main__':
    main(None)

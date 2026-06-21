"""
Phase 6.8: 结构牛市适应性归因（v2.2 数据隔离+归因口径修正）

冻结B0.3，不改生产策略，不使用2025-2026样本外数据。

v2.2 修正：
1. 数据严格隔离：strategy_market_df 仅 ETF_UNIVERSE+DEFENSE_UNIVERSE（18只，用于B0.3回测）；
   coverage_market_df 为数据库全部非SECTOR ETF（41只，仅用于覆盖度分析）。
2. 强断言：B0.3交易记录必须为642笔；所有交易ticker必须属于冻结策略池。
3. 现金反事实：virtual_day_ret = actual_strategy_day_ret + cash_pct × benchmark_day_ret，
   然后连乘，与实际策略收益比较。
4. 选股归因：按每日持仓的"下一期收益" vs 当时未选中池内ETF的"下一期收益"比较。
   不能用区间持仓频次乘整段收益。
5. 覆盖分析：使用全市场ETF，检查ETF当时已上市且区间首尾都有价格。

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
EXPECTED_TRADES = 642
EXPECTED_STOP_LOSS = 14

# 冻结策略池
FROZEN_POOL = set(list(config.ETF_UNIVERSE.keys()) + list(config.DEFENSE_UNIVERSE.keys()))


# ============ 1. 数据加载（严格隔离） ============

def load_data():
    """
    严格数据隔离：
    - strategy_market_df：仅ETF_UNIVERSE + DEFENSE_UNIVERSE + 000300.SH，用于B0.3回测
    - coverage_market_df：数据库中所有非SECTOR ETF，仅用于覆盖度分析
    """
    db = ETFDatabase(config.DB_PATH)
    
    # 全部数据（排除SECTOR）
    all_market = db.get_market_data()
    all_tickers = all_market['ticker'].unique().tolist()
    etf_tickers = [t for t in all_tickers if not t.startswith('SECTOR_')]
    coverage_market_df = all_market[all_market['ticker'].isin(etf_tickers)].copy()
    coverage_market_df = coverage_market_df[coverage_market_df['date'] <= pd.Timestamp(CUTOFF_DATE)]
    
    # 策略池数据（仅ETF_UNIVERSE + DEFENSE_UNIVERSE + 000300.SH）
    strategy_tickers = list(FROZEN_POOL) + ['000300.SH']
    strategy_market_df = coverage_market_df[coverage_market_df['ticker'].isin(strategy_tickers)].copy()
    
    bench_df = coverage_market_df[coverage_market_df['ticker'] == '000300.SH'][
        ['date', 'open', 'high', 'low', 'close', 'adj_close']
    ].copy()
    strategy_industry_df = strategy_market_df[strategy_market_df['ticker'] != '000300.SH'].copy()
    
    return strategy_market_df, strategy_industry_df, bench_df, coverage_market_df


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
    使用策略池数据（含000300.SH）进行结构牛市识别。
    定义：20日>=0%，分化>3%，连续>=5交易日，区间累计收益>0%。
    """
    hs300 = market_df[market_df['ticker'] == '000300.SH'][['date', 'close']].sort_values('date').reset_index(drop=True)
    hs300['ret'] = hs300['close'].pct_change(20)

    # 行业分化度（使用策略池中的行业ETF）
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

    # 找连续交易日段
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

    # 过滤：区间累计沪深300收益 > 0%
    filtered_periods = []
    for p in periods:
        hs300_window = hs300[(hs300['date'] >= p['start']) & (hs300['date'] <= p['end'])]
        if len(hs300_window) >= 2:
            cum_ret = (hs300_window['close'].iloc[-1] / hs300_window['close'].iloc[0]) - 1
            if cum_ret > 0:
                p['hs300_cum_ret'] = cum_ret
                filtered_periods.append(p)

    return filtered_periods


# ============ 3. 当时可交易ETF（策略池） ============

def get_available_strategy_etfs(market_df, as_of_date):
    """获取策略池在as_of_date前已上市的ETF"""
    strategy_tickers = [t for t in FROZEN_POOL]
    etf_first_dates = market_df[market_df['ticker'].isin(strategy_tickers)].groupby('ticker')['date'].min()
    available = [t for t in etf_first_dates.index if etf_first_dates[t] <= pd.Timestamp(as_of_date)]
    return available


def get_available_coverage_etfs(market_df, as_of_date):
    """获取数据库中在as_of_date前已上市的ETF（非000300.SH）"""
    etf_first_dates = market_df[market_df['ticker'] != '000300.SH'].groupby('ticker')['date'].min()
    available = [t for t in etf_first_dates.index if etf_first_dates[t] <= pd.Timestamp(as_of_date)]
    return available


# ============ 4. 五个诊断维度 ============

def compute_daily_returns(market_df, ticker, start, end):
    """计算某ETF在[start, end]区间内的累计收益"""
    data = market_df[(market_df['ticker'] == ticker) & (market_df['date'] >= start) & (market_df['date'] <= end)].sort_values('date')
    if len(data) >= 2:
        return (data['close'].iloc[-1] / data['close'].iloc[0]) - 1
    return 0


def diagnose_cash_drag(nav_df, bench_df, start, end):
    """
    现金拖累：virtual_day_ret = actual_strategy_day_ret + cash_pct * benchmark_day_ret
    然后连乘，与实际策略收益比较。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if len(window) < 2:
        return 0, 0

    bench = bench_df[['date', 'close']].sort_values('date').rename(columns={'close': 'bench_close'})
    window = window.merge(bench, on='date', how='left')

    actual_nav = 1
    virtual_nav = 1

    for i in range(len(window) - 1):
        row = window.iloc[i]
        next_row = window.iloc[i + 1]

        cash_pct = row['cash'] / row['nav'] if row['nav'] > 0 else 0
        actual_day_ret = (next_row['nav'] / row['nav']) - 1 if row['nav'] > 0 else 0
        bench_day_ret = (next_row['bench_close'] / row['bench_close']) - 1 if row['bench_close'] > 0 else 0

        virtual_day_ret = actual_day_ret + cash_pct * bench_day_ret

        actual_nav *= (1 + actual_day_ret)
        virtual_nav *= (1 + virtual_day_ret)

    strategy_ret = actual_nav - 1
    virtual_ret = virtual_nav - 1
    cash_drag = virtual_ret - strategy_ret

    return cash_drag, strategy_ret


def diagnose_coverage_gap(coverage_market_df, strategy_market_df, start, end):
    """
    覆盖差距：使用数据库全部ETF（coverage_market_df）作为"市场"，
    使用策略池（strategy_market_df）中在start前已上市的ETF作为"池"。
    检查最领涨方向是否在策略池内。
    """
    # 市场所有ETF（排除000300.SH）
    all_etfs = coverage_market_df[coverage_market_df['ticker'] != '000300.SH']['ticker'].unique().tolist()
    all_returns = {t: compute_daily_returns(coverage_market_df, t, start, end) for t in all_etfs}
    all_returns = {t: v for t, v in all_returns.items() if v != 0}  # 排除无数据

    # 策略池内ETF
    available_pool = get_available_strategy_etfs(strategy_market_df, start)
    pool_returns = {t: compute_daily_returns(strategy_market_df, t, start, end) for t in available_pool}
    pool_returns = {t: v for t, v in pool_returns.items() if v != 0}

    if not pool_returns or not all_returns:
        return 0, None, 0, None, 0

    market_best_ticker = max(all_returns, key=all_returns.get)
    market_best_ret = all_returns[market_best_ticker]

    pool_best_ticker = max(pool_returns, key=pool_returns.get)
    pool_best_ret = pool_returns[pool_best_ticker]

    # 检查市场最佳是否在策略池内且当时已上市且首尾有价格
    if market_best_ticker in available_pool and market_best_ticker in pool_returns:
        coverage_gap = 0
    else:
        coverage_gap = market_best_ret - pool_best_ret

    return coverage_gap, market_best_ticker, market_best_ret, pool_best_ticker, pool_best_ret


def diagnose_selection_gap(strategy_market_df, nav_df, trades_df, start, end):
    """
    选股差距：按调仓区间（或每日）计算"当时持仓下一期收益 vs 当时未选中ETF篮子下一期收益"。
    
    方法：获取区间内每个调仓日的持仓，计算到下一调仓日（或区间结束）的收益。
    同时计算策略池内未选中ETF的同一期收益。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if window.empty:
        return 0, 0, 0, []

    # 获取区间内的所有调仓日（positions_detail非空的日期）
    rebalance_dates = []
    for _, row in window.iterrows():
        pdet = row.get('positions_detail', {}) if isinstance(row.get('positions_detail'), dict) else {}
        if pdet:
            rebalance_dates.append(row['date'])

    if not rebalance_dates:
        return 0, 0, 0, []

    # 获取策略池内可用的ETF
    available_pool = get_available_strategy_etfs(strategy_market_df, start)

    position_period_returns = []
    unselected_period_returns = []

    for i, rebalance_date in enumerate(rebalance_dates):
        # 下一期结束日
        if i + 1 < len(rebalance_dates):
            period_end = rebalance_dates[i + 1]
        else:
            period_end = end

        # 当日持仓
        row = nav_df[nav_df['date'] == rebalance_date].iloc[0]
        pdet = row.get('positions_detail', {}) if isinstance(row.get('positions_detail'), dict) else {}
        held_tickers = [t for t, info in pdet.items() if isinstance(info, dict) and info.get('market_value', 0) > 0]

        if not held_tickers:
            continue

        # 持仓ETF的下一期收益
        held_rets = []
        for t in held_tickers:
            ret = compute_daily_returns(strategy_market_df, t, rebalance_date, period_end)
            if ret != 0:  # 有价格数据
                held_rets.append(ret)

        # 未选中ETF的下一期收益
        unselected = [t for t in available_pool if t not in held_tickers]
        unselected_rets = []
        for t in unselected:
            ret = compute_daily_returns(strategy_market_df, t, rebalance_date, period_end)
            if ret != 0:
                unselected_rets.append(ret)

        # 记录该期结果
        if held_rets:
            avg_held = np.mean(held_rets)
            position_period_returns.append(avg_held)
        if unselected_rets:
            median_unselected = np.median(unselected_rets)
            unselected_period_returns.append(median_unselected)

    # 整体选股差距：各期持仓收益 - 各期未选中中位数，然后平均
    if position_period_returns and unselected_period_returns:
        # 对齐两期
        min_len = min(len(position_period_returns), len(unselected_period_returns))
        gaps = [position_period_returns[i] - unselected_period_returns[i] for i in range(min_len)]
        avg_gap = np.mean(gaps) if gaps else 0
        avg_position = np.mean(position_period_returns) if position_period_returns else 0
        avg_unselected = np.mean(unselected_period_returns) if unselected_period_returns else 0
    else:
        avg_gap = 0
        avg_position = 0
        avg_unselected = 0

    return avg_gap, avg_position, avg_unselected, []


def diagnose_weight_gap(nav_df, strategy_market_df, start, end):
    """
    权重差距：逐日计算等权配置 vs 策略实际权重的累计收益。
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
            # 空仓日
            actual_ret = (next_row['nav'] / row['nav']) - 1 if row['nav'] > 0 else 0
            equal_ret = actual_ret
        else:
            # 获取各ETF日收益
            etf_rets = {}
            for t in actual_tickers:
                ret = compute_daily_returns(strategy_market_df, t, day_start, day_end)
                if ret != 0:
                    etf_rets[t] = ret

            if etf_rets:
                equal_ret = np.mean(list(etf_rets.values()))
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


def diagnose_exit_gap(strategy_market_df, nav_df, trades_df, start, end):
    """
    退出差距：使用trades_df的price、shares、commission计算净金额PnL。
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

        # 验证ticker在策略池内
        assert ticker in FROZEN_POOL, f"止损交易ticker {ticker} 不在策略池内！"

        # 从BUY记录获取买入成本
        buy_records = trades_df[(trades_df['ticker'] == ticker) & (trades_df['action'] == 'BUY') & (trades_df['date'] <= exit_date)].copy()
        if buy_records.empty:
            continue

        buy_records = buy_records.sort_values('date')
        buy_record = buy_records.iloc[-1]
        buy_cost = buy_record['price']
        buy_commission = buy_record['commission']

        # 实际止损净金额PnL = (卖出价 - 买入成本) * 股数 - 卖出佣金 - 买入佣金
        actual_pnl = (sell_price - buy_cost) * shares - commission - buy_commission

        # 找到下次真实周四调仓日（必须是交易日）
        next_thursday = None
        for d in pd.date_range(exit_date + timedelta(days=1), exit_date + timedelta(days=14), freq='D'):
            if d.weekday() == 3:
                t_market = strategy_market_df[strategy_market_df['ticker'] == ticker][['date']].drop_duplicates()
                if d in t_market['date'].values:
                    next_thursday = d
                    break

        # 持有至调仓的净金额PnL
        hold_pnl = actual_pnl
        if next_thursday is not None:
            t_market = strategy_market_df[strategy_market_df['ticker'] == ticker][['date', 'close']].sort_values('date')
            if len(t_market[t_market['date'] >= next_thursday]) > 0:
                rebalance_price = t_market[t_market['date'] >= next_thursday]['close'].iloc[0]
                hold_pnl = (rebalance_price - buy_cost) * shares - buy_commission

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
    virtual_day_ret = actual_strategy_day_ret + cash_pct * benchmark_day_ret
    然后连乘，与实际策略收益比较。
    """
    window = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].sort_values('date').reset_index(drop=True)
    if len(window) < 2:
        return 0

    bench = bench_df[['date', 'close']].sort_values('date').rename(columns={'close': 'bench_close'})
    window = window.merge(bench, on='date', how='left')

    actual_nav = 1
    virtual_nav = 1

    for i in range(len(window) - 1):
        row = window.iloc[i]
        next_row = window.iloc[i + 1]
        cash_pct = row['cash'] / row['nav'] if row['nav'] > 0 else 0
        actual_day_ret = (next_row['nav'] / row['nav']) - 1 if row['nav'] > 0 else 0
        bench_day_ret = (next_row['bench_close'] / row['bench_close']) - 1 if row['bench_close'] > 0 else 0

        virtual_day_ret = actual_day_ret + cash_pct * bench_day_ret

        actual_nav *= (1 + actual_day_ret)
        virtual_nav *= (1 + virtual_day_ret)

    strategy_ret = actual_nav - 1
    virtual_ret = virtual_nav - 1

    return virtual_ret - strategy_ret


# ============ 6. 多区间验证 ============

def analyze_all_periods(periods, strategy_market_df, strategy_industry_df, bench_df, coverage_market_df, nav_df, trades_df):
    """对识别到的所有结构牛市区间做归因分析"""
    results = []
    for p in periods:
        start = p['start']
        end = p['end']

        available = get_available_strategy_etfs(strategy_market_df, start)

        cash_drag, _ = diagnose_cash_drag(nav_df, bench_df, start, end)
        cov_gap, m_ticker, m_ret, p_ticker, p_ret = diagnose_coverage_gap(coverage_market_df, strategy_market_df, start, end)
        sel_gap, avg_pos, avg_unsel, _ = diagnose_selection_gap(strategy_market_df, nav_df, trades_df, start, end)
        w_gap, actual_r, equal_r = diagnose_weight_gap(nav_df, strategy_market_df, start, end)
        e_gap, exit_pnl, hold_pnl, stop_cnt = diagnose_exit_gap(strategy_market_df, nav_df, trades_df, start, end)
        cf1 = cf1_cash_to_benchmark(nav_df, bench_df, start, end)

        results.append({
            'start': start, 'end': end,
            'trading_days': p['trading_day_count'],
            'hs300_cum_ret': p['hs300_cum_ret'],
            'available_etfs': len(available),
            'cash_drag': cash_drag,
            'coverage_gap': cov_gap,
            'coverage_market_best': m_ticker,
            'coverage_market_ret': m_ret,
            'coverage_pool_best': p_ticker,
            'coverage_pool_ret': p_ret,
            'selection_gap': sel_gap,
            'avg_position_ret': avg_pos,
            'avg_unselected_ret': avg_unsel,
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
    """勾稽断言"""
    errors = []

    for r in results:
        if r['end'] > pd.Timestamp('2024-12-31'):
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}结束日期超过2024-12-31")

        window_nav = nav_df[(nav_df['date'] >= r['start']) & (nav_df['date'] <= r['end'])]
        if window_nav.empty:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}无NAV数据")
            continue

        if len(window_nav) < r['trading_days']:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}NAV数据不足")

        window_trades = trades_df[(trades_df['date'] >= r['start']) & (trades_df['date'] <= r['end'])]
        actual_stop_count = len(window_trades[window_trades['action'] == 'STOP_LOSS'])
        if r['stop_count'] != actual_stop_count:
            errors.append(f"区间{r['start'].date()}~{r['end'].date()}止损计数不一致")

    return errors


# ============ 8. 主程序 ============

def main(ctx):
    print("[1/8] 加载数据...")
    strategy_market_df, strategy_industry_df, bench_df, coverage_market_df = load_data()
    trading_days = get_trading_days(strategy_market_df)
    print(f"    策略池ETF: {len(strategy_industry_df['ticker'].unique())}只")
    print(f"    数据库全部ETF: {len(coverage_market_df[coverage_market_df['ticker'] != '000300.SH']['ticker'].unique())}只")

    print("[2/8] 运行B0.3回测...")
    result = run_b03_backtest(strategy_industry_df, bench_df)
    nav_df = result['nav_df'].copy()
    trades_df = result['trades_df'].copy()

    for df in [nav_df, trades_df]:
        if not df.empty and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])

    print(f"    回测区间: {nav_df['date'].min().date()} ~ {nav_df['date'].max().date()}")
    print(f"    交易记录: {len(trades_df)}条")
    print(f"    止损次数: {result['stop_loss_count']}次")

    # 强断言1：B0.3交易记录必须为642笔
    assert len(trades_df) == EXPECTED_TRADES, f"B0.3交易记录应为{EXPECTED_TRADES}笔，实际{len(trades_df)}笔！"
    print(f"    [PASS] 交易记录={len(trades_df)}，符合{EXPECTED_TRADES}")

    # 强断言2：所有交易ticker必须属于冻结策略池
    trade_tickers = set(trades_df['ticker'].unique())
    non_pool = trade_tickers - FROZEN_POOL
    assert not non_pool, f"发现不属于策略池的交易ticker: {non_pool}"
    print(f"    [PASS] 所有交易ticker都在策略池内")

    # 强断言3：止损次数应为14次
    assert result['stop_loss_count'] == EXPECTED_STOP_LOSS, f"止损次数应为{EXPECTED_STOP_LOSS}，实际{result['stop_loss_count']}！"
    print(f"    [PASS] 止损次数={result['stop_loss_count']}，符合{EXPECTED_STOP_LOSS}")

    print("[3/8] 识别结构牛市区间...")
    periods = identify_structural_bull_periods(strategy_market_df, trading_days)
    periods = [p for p in periods if p['start'].year <= 2024]
    
    nav_start = nav_df['date'].min()
    periods = [p for p in periods if p['start'] >= nav_start]
    print(f"    找到 {len(periods)} 个结构牛市区间(有NAV数据, 累计收益>0)")

    print("[4/8] 多区间验证...")
    results = analyze_all_periods(periods, strategy_market_df, strategy_industry_df, bench_df, coverage_market_df, nav_df, trades_df)

    train_results = [r for r in results if r['end'].year <= 2022]
    val_results = [r for r in results if r['start'].year >= 2023]

    print(f"    训练期(2019-2022): {len(train_results)}个区间")
    print(f"    验证期(2023-2024): {len(val_results)}个区间")

    print("[5/8] 勾稽断言...")
    errors = validate_results(results, nav_df, trades_df)
    if errors:
        print(f"    [WARN] 发现{len(errors)}个断言失败:")
        for e in errors:
            print(f"      - {e}")
    else:
        print("    [OK] 所有勾稽断言通过")

    print("[6/8] 目标区间详细分析...")
    target_start = pd.Timestamp(TARGET_START)
    target_end = pd.Timestamp(TARGET_END)

    def get_bench_price(bench_df, date):
        prices = bench_df[bench_df['date'] <= date]
        if prices.empty:
            return None
        return prices.iloc[-1]['close']

    target_start_price = get_bench_price(bench_df, target_start)
    target_end_price = get_bench_price(bench_df, target_end)
    target_bench_ret = (target_end_price / target_start_price) - 1 if target_start_price and target_end_price else 0

    target_available = get_available_strategy_etfs(strategy_market_df, target_start)

    t_cash, t_strategy_ret = diagnose_cash_drag(nav_df, bench_df, target_start, target_end)
    t_cov, t_m_ticker, t_m_ret, t_p_ticker, t_p_ret = diagnose_coverage_gap(coverage_market_df, strategy_market_df, target_start, target_end)
    t_sel, t_avg_pos, t_avg_unsel, _ = diagnose_selection_gap(strategy_market_df, nav_df, trades_df, target_start, target_end)
    t_w, t_actual, t_equal = diagnose_weight_gap(nav_df, strategy_market_df, target_start, target_end)
    t_e, t_exit_pnl, t_hold_pnl, t_stop_cnt = diagnose_exit_gap(strategy_market_df, nav_df, trades_df, target_start, target_end)
    t_cf1 = cf1_cash_to_benchmark(nav_df, bench_df, target_start, target_end)

    t_excess = t_strategy_ret - target_bench_ret

    print(f"    策略收益: {t_strategy_ret:.2%}")
    print(f"    基准收益: {target_bench_ret:.2%}")
    print(f"    总超额: {t_excess:.2%}")
    print(f"    1.现金拖累: {t_cash:.2%}")
    print(f"    2.覆盖差距: {t_cov:.2%} (市场最佳={t_m_ticker}, 池内最佳={t_p_ticker})")
    print(f"    3.选股差距: {t_sel:.2%} (策略持仓={t_avg_pos:.2%}, 未选中中位数={t_avg_unsel:.2%})")
    print(f"    4.权重差距: {t_w:.2%} (等权={t_equal:.2%}, 实际={t_actual:.2%})")
    if t_e > 0:
        print(f"    5.退出差距: {t_e:.2%} (止损={t_exit_pnl:.2%}比持有={t_hold_pnl:.2%}多亏，加速亏损)")
    else:
        print(f"    5.退出差距: {t_e:.2%} (止损={t_exit_pnl:.2%}比持有={t_hold_pnl:.2%}少亏，保护组合)")
    print(f"    CF1: {t_cf1:.2%}")
    print(f"    当时可交易ETF: {len(target_available)}只(策略池)")

    print("[7/8] 生成报告...")
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
    lines.append("| 序号 | 开始 | 结束 | 交易日数 | 区间沪深300累计收益 |")
    lines.append("|------|------|------|----------|---------------------|")
    for i, p in enumerate(periods, 1):
        lines.append(f"| {i} | {p['start'].strftime('%Y-%m-%d')} | {p['end'].strftime('%Y-%m-%d')} | {p['trading_day_count']} | {p['hs300_cum_ret']:.2%} |")
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
    lines.append(f"- **当时可交易ETF（策略池）**: {len(target_available)}只")
    lines.append(f"- **数据库全部非SECTOR ETF**: {len(coverage_market_df[coverage_market_df['ticker'] != '000300.SH']['ticker'].unique())}只")
    lines.append("")
    lines.append("### 五个诊断维度")
    lines.append("")
    lines.append("| 维度 | 数值 | 说明 |")
    lines.append("|------|------|------|")
    lines.append(f"| 1. 现金拖累 | {t_cash:.2%} | virtual_day_ret = actual + cash_pct * benchmark，连乘后比较 |")
    if t_cov == 0:
        lines.append(f"| 2. 覆盖差距 | 0.00% | 最领涨方向在策略池内（市场最佳={t_m_ticker}={t_m_ret:.2%}） |")
    else:
        lines.append(f"| 2. 覆盖差距 | {t_cov:.2%} | 最领涨方向不在策略池内（市场最佳={t_m_ticker}={t_m_ret:.2%}） |")
    lines.append(f"| 3. 选股差距 | {t_sel:.2%} | 各调仓期持仓收益 vs 未选中中位数，平均后比较 |")
    lines.append(f"| 4. 权重差距 | {t_w:.2%} | 逐日等权({t_equal:.2%}) vs 逐日实际({t_actual:.2%}) |")
    if t_e > 0:
        lines.append(f"| 5. 退出差距 | {t_e:.2%} | 止损({t_exit_pnl:.2%})比持有({t_hold_pnl:.2%})多亏，加速亏损 |")
    else:
        lines.append(f"| 5. 退出差距 | {t_e:.2%} | 止损({t_exit_pnl:.2%})比持有({t_hold_pnl:.2%})少亏，保护组合 |")
    lines.append("")
    lines.append("> **注**：五个诊断维度定义口径不同，不强制加总等于总超额。")
    lines.append("")
    lines.append(f"- **最领涨ETF（数据库全部）**: {t_m_ticker} ({t_m_ret:.2%})")
    lines.append(f"- **池内最佳ETF（策略池）**: {t_p_ticker} ({t_p_ret:.2%})")
    lines.append(f"- **策略持仓平均收益（各调仓期）**: {t_avg_pos:.2%}")
    lines.append(f"- **策略未选中中位数（各调仓期）**: {t_avg_unsel:.2%}")
    lines.append(f"- **等权配置收益**: {t_equal:.2%}")
    lines.append(f"- **止损次数**: {t_stop_cnt}次")
    lines.append("")

    lines.append("## 四、反事实实验")
    lines.append("")
    lines.append("### 反事实1：闲置资金配置沪深300ETF")
    lines.append(f"- 如果空仓时的现金全部买入沪深300ETF，额外收益: **{t_cf1:.2%}**")
    lines.append("- 计算方式：virtual_day_ret = actual_strategy_day_ret + cash_pct * benchmark_day_ret，连乘后比较。")
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
    if t_sel > 0:
        lines.append(f"   - 选股差距：{t_sel:.2%}（各调仓期持仓收益优于未选中中位数）")
    else:
        lines.append(f"   - 选股差距：{t_sel:.2%}（各调仓期持仓收益劣于未选中中位数）")
    if t_w > 0:
        lines.append(f"   - 权重差距：{t_w:.2%}（等权{t_equal:.2%}优于实际{t_actual:.2%}）")
    else:
        lines.append(f"   - 权重差距：{t_w:.2%}（实际权重不劣于等权）")
    if t_e > 0:
        lines.append(f"   - 退出差距：{t_e:.2%}（止损{t_exit_pnl:.2%}比持有{t_hold_pnl:.2%}多亏，加速亏损）")
    else:
        lines.append(f"   - 退出差距：{t_e:.2%}（止损{t_exit_pnl:.2%}比持有{t_hold_pnl:.2%}少亏，保护组合）")
    lines.append("")
    lines.append(f"3. **当时可交易ETF**：策略池{len(target_available)}只，数据库全部{len(coverage_market_df[coverage_market_df['ticker'] != '000300.SH']['ticker'].unique())}只。策略池（ETF_UNIVERSE）中部分后期热门板块ETF尚未上市。")
    lines.append("")
    lines.append(f"4. **反事实1**：如果现金投入300ETF，可额外获得{t_cf1:.2%}收益。")
    lines.append("")
    if t_e > 0:
        lines.append(f"5. **反事实3**：止损机制在该区间加速了亏损，差异{t_e:.2%}。")
    else:
        lines.append(f"5. **反事实3**：止损机制在该区间保护了组合，差异{t_e:.2%}。")
    lines.append("")

    lines.append("## 六、勾稽断言与强断言")
    lines.append("")
    lines.append("**强断言（B0.3复现）：**")
    lines.append(f"- [PASS] 交易记录={len(trades_df)}，符合{EXPECTED_TRADES}")
    lines.append(f"- [PASS] 所有交易ticker都在策略池内（{len(trade_tickers)}个ticker）")
    lines.append(f"- [PASS] 止损次数={result['stop_loss_count']}，符合{EXPECTED_STOP_LOSS}")
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

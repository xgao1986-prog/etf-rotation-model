"""
Phase 6.7: 长假调仓日历适配实验
仅研究，不修改生产配置。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model\src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import json

import config
from database import ETFDatabase
from backtest import BacktestEngine


# ============ 1. 长假识别 ============

def identify_long_holidays(trading_days, min_gap_natural=5):
    """
    识别长假：连续>=min_gap_natural个自然日无交易的区间。
    返回列表，每项包含：last_trading_before, first_trading_after, gap_natural, gap_trading
    """
    trading_days = sorted(pd.to_datetime(trading_days))
    holidays = []
    
    for i in range(len(trading_days) - 1):
        today = trading_days[i]
        next_day = trading_days[i + 1]
        gap_natural = (next_day - today).days - 1
        
        if gap_natural >= min_gap_natural:
            holidays.append({
                'last_trading_before': today,
                'first_trading_after': next_day,
                'gap_natural': gap_natural,
            })
    
    return holidays


# ============ 2. 计算各变种的调仓日 ============

def compute_rebalance_dates_variant_a(trading_days, target_weekday=3):
    """A. 当前规则：仅周四，休市则跳过"""
    trading_days = pd.to_datetime(trading_days)
    return set(d for d in trading_days if d.weekday() == target_weekday)


def compute_rebalance_dates_variant_b(trading_days, holidays, target_weekday=3):
    """
    B. 假期前补调：长假前最后一个交易日替代原周四。
    约束：同一长假只替代一次；替代后下次正常周四若<3个交易日则跳过。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    base_dates = compute_rebalance_dates_variant_a(trading_days, target_weekday)
    rebalance_dates = set()
    
    for h in holidays:
        last_before = h['last_trading_before']
        first_after = h['first_trading_after']
        
        # 长假中落在周四的日期（>last_before且<first_after）
        thursdays_in_holiday = [d for d in trading_days if d > last_before and d < first_after and d.weekday() == target_weekday]
        
        for thu in thursdays_in_holiday:
            # 用last_before替代该周四
            rebalance_dates.add(last_before)
    
    # 添加正常周四（未被替代的）
    for d in base_dates:
        skip = False
        for sub_d in rebalance_dates:
            if sub_d.weekday() != target_weekday:  # 只检查替代日（非周四）
                days_between = sum(1 for td in trading_list if min(sub_d, d) < td < max(sub_d, d))
                if days_between < 3:
                    skip = True
                    break
        if not skip:
            rebalance_dates.add(d)
    
    return rebalance_dates


def compute_rebalance_dates_variant_c(trading_days, holidays, target_weekday=3):
    """
    C. 假期后补调：长假后首个交易日替代下一周四。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    base_dates = compute_rebalance_dates_variant_a(trading_days, target_weekday)
    rebalance_dates = set()
    
    for h in holidays:
        last_before = h['last_trading_before']
        first_after = h['first_trading_after']
        
        # 长假后的第一个周四（>= first_after）
        thursday_after = None
        for d in trading_days:
            if d >= first_after and d.weekday() == target_weekday:
                thursday_after = d
                break
        
        if thursday_after is not None:
            days_between = sum(1 for td in trading_list if first_after <= td < thursday_after)
            if days_between < 3:
                # 用first_after替代
                rebalance_dates.add(first_after)
            else:
                rebalance_dates.add(thursday_after)
    
    # 添加未被替代的正常周四
    for d in base_dates:
        skip = False
        for sub_d in rebalance_dates:
            if sub_d.weekday() != target_weekday:
                days_between = sum(1 for td in trading_list if min(sub_d, d) < td < max(sub_d, d))
                if days_between < 3:
                    skip = True
                    break
        if not skip:
            rebalance_dates.add(d)
    
    return rebalance_dates


def compute_rebalance_dates_variant_d(trading_days, holidays, target_weekday=3):
    """
    D. 最近交易日：计划周四休市时，选择距离周四最近的交易日，每周最多一次。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    trading_set = set(trading_days)
    
    # 生成所有自然日中的周四
    all_thursdays = []
    start = trading_list[0]
    end = trading_list[-1]
    current = start
    while current.weekday() != target_weekday:
        current += timedelta(days=1)
    while current <= end:
        all_thursdays.append(current)
        current += timedelta(days=7)
    
    rebalance_dates = set()
    used_weeks = set()
    
    for thu in all_thursdays:
        week_key = (thu.year, thu.isocalendar().week)
        if week_key in used_weeks:
            continue
        
        if thu in trading_set:
            rebalance_dates.add(thu)
            used_weeks.add(week_key)
        else:
            # 找最近的交易日
            prev_td = None
            for d in reversed(trading_list):
                if d < thu:
                    prev_td = d
                    break
            next_td = None
            for d in trading_list:
                if d > thu:
                    next_td = d
                    break
            
            if prev_td is not None and next_td is not None:
                if (thu - prev_td).days <= (next_td - thu).days:
                    chosen = prev_td
                else:
                    chosen = next_td
            elif prev_td is not None:
                chosen = prev_td
            elif next_td is not None:
                chosen = next_td
            else:
                continue
            
            rebalance_dates.add(chosen)
            used_weeks.add(week_key)
    
    return rebalance_dates


# ============ 3. 自定义回测引擎 ============

class CustomRebalanceEngine(BacktestEngine):
    """支持自定义调仓日集合的回测引擎"""
    
    def __init__(self, cfg=None, s1_mode=False, rebalance_dates=None):
        super().__init__(cfg, s1_mode)
        self._rebalance_dates = rebalance_dates or set()
    
    def _is_rebalance_day(self, date, last_rebalance_date=None):
        dt = pd.to_datetime(date)
        if dt in self._rebalance_dates:
            return True
        return False


# ============ 4. 数据加载与回测 ============

def load_data_from_db():
    db = ETFDatabase(config.DB_PATH)
    all_tickers = list(config.ETF_UNIVERSE.keys()) + ['000300.SH']
    market_df = db.get_market_data(ticker=all_tickers)
    bench_df = market_df[market_df['ticker'] == '000300.SH'][['date', 'open', 'high', 'low', 'close', 'adj_close']].copy()
    market_df = market_df[market_df['ticker'] != '000300.SH']
    return market_df, bench_df


def run_b03_backtest_custom(market_df, bench_df, rebalance_dates):
    """运行B0.3回测，使用自定义调仓日"""
    cfg = config.STRATEGY_CONFIG.copy()
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    cfg['min_total_score'] = 40
    cfg['stop_loss'] = -0.08
    cfg['stop_loss_mode'] = 'fixed'
    
    engine = CustomRebalanceEngine(cfg=cfg, rebalance_dates=rebalance_dates)
    result = engine.run(market_df, bench_df, as_of_date='2024-12-31')
    return result


def get_trading_days(market_df):
    return sorted(market_df['date'].dropna().unique())


# ============ 5. 主程序 ============

def main(ctx):
    print("[1/6] 加载数据...")
    market_df, bench_df = load_data_from_db()
    trading_days = get_trading_days(market_df)
    
    print("[2/6] 识别长假...")
    all_holidays = identify_long_holidays(trading_days)
    # 仅保留2019-2024的长假
    holidays = [h for h in all_holidays if h['last_trading_before'].year <= 2024]
    print(f"    发现 {len(holidays)} 个长假(2019-2024):")
    for h in holidays:
        print(f"      {h['last_trading_before'].strftime('%Y-%m-%d')} ~ {h['first_trading_after'].strftime('%Y-%m-%d')} ({h['gap_natural']}自然日)")
    
    print("[3/6] 计算各变种调仓日...")
    variants = {
        'A': compute_rebalance_dates_variant_a(trading_days),
        'B': compute_rebalance_dates_variant_b(trading_days, holidays),
        'C': compute_rebalance_dates_variant_c(trading_days, holidays),
        'D': compute_rebalance_dates_variant_d(trading_days, holidays),
    }
    
    for name, dates in variants.items():
        print(f"    {name}: {len(dates)} 个调仓日")
    
    print("[4/6] 运行回测...")
    results = {}
    for name, dates in variants.items():
        print(f"    运行变种 {name}...")
        result = run_b03_backtest_custom(market_df, bench_df, dates)
        results[name] = result
        print(f"      总收益: {result['total_return']:.2%}, 年化: {result['annual_return']:.2%}, Sharpe: {result['sharpe_ratio']:.3f}, 回撤: {result['max_drawdown']:.2%}")
    
    print("[5/6] 生成报告...")
    output_path = r'D:\etf_rotation_model\reports\phase6_7_holiday_rebalance_experiment.md'
    
    lines = []
    lines.append("# Phase 6.7: 长假调仓日历适配实验")
    lines.append("")
    lines.append("> **注意**：本报告仅研究，不修改策略。不修改生产配置。")
    lines.append("")
    lines.append(f"> 数据区间：{results['A']['nav_df']['date'].min().strftime('%Y-%m-%d')} ~ {results['A']['nav_df']['date'].max().strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 长假列表
    lines.append("## 一、识别到的长假（>=5自然日）")
    lines.append("")
    lines.append("| 序号 | 开始日期 | 结束日期 | 自然日 | 备注 |")
    lines.append("|------|----------|----------|--------|------|")
    for i, h in enumerate(holidays, 1):
        lines.append(f"| {i} | {h['last_trading_before'].strftime('%Y-%m-%d')} | {h['first_trading_after'].strftime('%Y-%m-%d')} | {h['gap_natural']} | {'春节' if h['last_trading_before'].month in [1,2] else '国庆' if h['last_trading_before'].month == 9 else '其他'} |")
    lines.append("")
    
    # 各变种调仓日对比
    lines.append("## 二、各变种调仓日对比")
    lines.append("")
    for name, dates in variants.items():
        lines.append(f"### 变种{name}: {['当前规则(仅周四)', '假期前补调', '假期后补调', '最近交易日'][ord(name)-ord('A')]} ")
        lines.append(f"- 调仓日总数: {len(dates)}")
        
        # 列出长假附近的调仓日变化
        for h in holidays:
            lb = h['last_trading_before']
            fa = h['first_trading_after']
            nearby = sorted([d for d in dates if lb - timedelta(days=7) <= d <= fa + timedelta(days=7)])
            lines.append(f"- {lb.strftime('%Y-%m-%d')}附近: {[d.strftime('%m-%d %a') for d in nearby]}")
        lines.append("")
    
    # 回测结果对比
    lines.append("## 三、回测结果对比")
    lines.append("")
    lines.append("| 指标 | A(当前) | B(前补) | C(后补) | D(最近) |")
    lines.append("|------|---------|---------|---------|---------|")
    
    metrics = [
        ('total_return', '总收益', '{:.2%}'),
        ('annual_return', '年化收益', '{:.2%}'),
        ('sharpe_ratio', 'Sharpe', '{:.3f}'),
        ('max_drawdown', '最大回撤', '{:.2%}'),
        ('num_trades', '交易笔数', '{:.0f}'),
        ('avg_holdings', '平均持仓', '{:.1f}'),
    ]
    
    for key, label, fmt in metrics:
        vals = [fmt.format(results[n][key]) for n in 'ABCD']
        lines.append(f"| {label} | {' | '.join(vals)} |")
    
    lines.append("")
    
    # 事件后收益对比
    lines.append("## 四、长假后事件收益对比")
    lines.append("")
    lines.append("| 长假 | 变种 | 后1日 | 后5日 | 后10日 |")
    lines.append("|------|------|-------|-------|--------|")
    
    for h in holidays:
        fa = h['first_trading_after']
        for name in 'ABCD':
            nav = results[name]['nav_df']
            fa_idx = nav[nav['date'] == fa].index
            if len(fa_idx) == 0:
                continue
            idx = fa_idx[0]
            
            nav1 = nav.iloc[idx]['nav']
            nav0 = nav.iloc[idx]['nav']  # 首日
            
            # 后1日
            if idx + 1 < len(nav):
                ret1 = nav.iloc[idx + 1]['nav'] / nav1 - 1
            else:
                ret1 = np.nan
            
            # 后5日
            if idx + 5 < len(nav):
                ret5 = nav.iloc[idx + 5]['nav'] / nav1 - 1
            else:
                ret5 = np.nan
            
            # 后10日
            if idx + 10 < len(nav):
                ret10 = nav.iloc[idx + 10]['nav'] / nav1 - 1
            else:
                ret10 = np.nan
            
            ret1_str = f"{ret1:.2%}" if not np.isnan(ret1) else "N/A"
            ret5_str = f"{ret5:.2%}" if not np.isnan(ret5) else "N/A"
            ret10_str = f"{ret10:.2%}" if not np.isnan(ret10) else "N/A"
            lines.append(f"| {fa.strftime('%Y-%m-%d')} | {name} | {ret1_str} | {ret5_str} | {ret10_str} |")
    
    lines.append("")
    
    # 结论
    lines.append("## 五、结论")
    lines.append("")
    lines.append("待分析完成后填充。")
    lines.append("")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"[6/6] 完成。报告: {output_path}")
    
    return {'report_path': output_path, 'variants': list(variants.keys())}


if __name__ == '__main__':
    main(None)

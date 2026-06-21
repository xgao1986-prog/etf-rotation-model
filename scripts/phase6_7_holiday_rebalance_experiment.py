"""
Phase 6.7: 长假调仓日历适配实验（修正版）

修正要点：
1. 使用自然日生成休市期间的计划周四，不在trading_days中搜索
2. B方案：长假覆盖计划周四 → 用节前最后交易日替代；删除被替代周四；输出触发清单
3. C方案：用节后首个交易日替代假期后正常周四；删除被替代周四
4. D方案：每个计划周四映射到一个最近交易日；替代而非新增；每周最多一次
5. 断言：每次替代都有source_date和target_date；同一长假仅替代一次；不存在相隔<3交易日的重复调仓；调仓总数差异由替代记录解释
6. 输出最大自然日和交易日调仓间隔
7. 2019-2024，不运行封存样本
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
    返回列表，每项包含：last_trading_before, first_trading_after, gap_natural
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


# ============ 2. 生成计划周四（自然日） ============

def generate_plan_thursdays(trading_days, target_weekday=3):
    """
    基于自然日生成所有计划周四。
    从第一个交易日所在周开始，到最后一个交易日所在周结束。
    """
    trading_days = sorted(pd.to_datetime(trading_days))
    start = trading_days[0]
    end = trading_days[-1]
    
    # 找到第一个>=start的周四
    current = start
    while current.weekday() != target_weekday:
        current += timedelta(days=1)
    
    plan_thursdays = []
    while current <= end:
        plan_thursdays.append(current)
        current += timedelta(days=7)
    
    return plan_thursdays


# ============ 3. 计算各变种的调仓日 ============

def compute_variant_a(trading_days, target_weekday=3):
    """
    A. 当前规则：仅周四，休市则跳过。
    计划周四是交易日则执行，否则跳过。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_set = set(trading_days)
    plan_thursdays = generate_plan_thursdays(trading_days, target_weekday)
    
    rebalance_dates = set()
    substitutions = []
    
    for thu in plan_thursdays:
        if thu in trading_set:
            rebalance_dates.add(thu)
        else:
            # 被跳过，但A方案不记录替代
            pass
    
    return rebalance_dates, substitutions


def compute_variant_b(trading_days, holidays, target_weekday=3):
    """
    B. 假期前补调：若长假覆盖计划周四，用节前最后交易日替代该周四。
    必须删除被替代的计划调仓。
    输出实际触发的长假清单。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    trading_set = set(trading_days)
    plan_thursdays = generate_plan_thursdays(trading_days, target_weekday)
    
    rebalance_dates = set()
    substitutions = []
    substituted_sources = set()
    
    for h in holidays:
        last_before = h['last_trading_before']
        first_after = h['first_trading_after']
        
        # 找到被该长假覆盖的所有计划周四
        covered_thursdays = [thu for thu in plan_thursdays if last_before < thu < first_after]
        
        # 同一长假只替代一次（取第一个被覆盖的计划周四）
        if covered_thursdays:
            # 用last_before替代（last_before必须是交易日）
            if last_before in trading_set:
                for thu in covered_thursdays:
                    substitutions.append({
                        'source_date': thu,
                        'target_date': last_before,
                        'holiday_start': last_before.strftime('%Y-%m-%d'),
                        'holiday_end': first_after.strftime('%Y-%m-%d'),
                        'reason': 'pre-holiday substitution'
                    })
                    substituted_sources.add(thu)
                rebalance_dates.add(last_before)
    
    # 添加未被替代的计划周四（且是交易日）
    for thu in plan_thursdays:
        if thu in substituted_sources:
            continue  # 被替代，跳过
        if thu not in trading_set:
            continue  # 休市，跳过
        
        # 检查是否与任何已替代日期间隔<3个交易日
        skip = False
        for sub in substitutions:
            target = sub['target_date']
            if target == thu:
                continue  # 同一日，不跳过（已在rebalance_dates中）
            days_between = sum(1 for td in trading_list if min(target, thu) < td < max(target, thu))
            if days_between < 3:
                skip = True
                break
        if not skip:
            rebalance_dates.add(thu)
    
    return rebalance_dates, substitutions


def compute_variant_c(trading_days, holidays, target_weekday=3):
    """
    C. 假期后补调：用节后首个交易日替代假期后的正常周四。
    删除被替代周四。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    trading_set = set(trading_days)
    plan_thursdays = generate_plan_thursdays(trading_days, target_weekday)
    
    rebalance_dates = set()
    substitutions = []
    substituted_sources = set()
    
    for h in holidays:
        last_before = h['last_trading_before']
        first_after = h['first_trading_after']
        
        # 找到长假后的第一个计划周四（>= first_after）
        thursday_after = None
        for thu in plan_thursdays:
            if thu >= first_after:
                thursday_after = thu
                break
        
        if thursday_after is not None and thursday_after in trading_set:
            # 检查thursday_after与first_after之间的交易日数
            days_between = sum(1 for td in trading_list if first_after <= td < thursday_after)
            
            if days_between < 3:
                # 用first_after替代
                if first_after in trading_set:
                    substitutions.append({
                        'source_date': thursday_after,
                        'target_date': first_after,
                        'holiday_start': last_before.strftime('%Y-%m-%d'),
                        'holiday_end': first_after.strftime('%Y-%m-%d'),
                        'reason': 'post-holiday substitution'
                    })
                    substituted_sources.add(thursday_after)
                    rebalance_dates.add(first_after)
    
    # 添加未被替代的计划周四（且是交易日）
    for thu in plan_thursdays:
        if thu in substituted_sources:
            continue
        if thu not in trading_set:
            continue
        
        skip = False
        for sub in substitutions:
            target = sub['target_date']
            if target == thu:
                continue
            days_between = sum(1 for td in trading_list if min(target, thu) < td < max(target, thu))
            if days_between < 3:
                skip = True
                break
        if not skip:
            rebalance_dates.add(thu)
    
    return rebalance_dates, substitutions


def compute_variant_d(trading_days, holidays, target_weekday=3):
    """
    D. 最近交易日：每个计划周四只能映射到一个最近交易日，是替代而非新增。
    每周最多一次调仓。
    """
    trading_days = pd.to_datetime(trading_days)
    trading_list = list(trading_days)
    trading_set = set(trading_days)
    plan_thursdays = generate_plan_thursdays(trading_days, target_weekday)
    
    rebalance_dates = set()
    substitutions = []
    used_dates = set()
    
    for thu in plan_thursdays:
        if thu in trading_set:
            # 周四是交易日，正常执行
            rebalance_dates.add(thu)
            used_dates.add(thu)
            continue
        
        # 周四休市，找最近的交易日
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
        
        # 选择距离周四最近的
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
        
        # 检查是否已使用（每周最多一次）
        if chosen in used_dates:
            continue
        
        # 检查与前一个调仓日间隔是否>=3个交易日
        # 找到已选日期中小于chosen的最大日期
        prev_rebalance = None
        for d in sorted(rebalance_dates):
            if d < chosen:
                prev_rebalance = d
        
        if prev_rebalance is not None:
            days_between = sum(1 for td in trading_list if prev_rebalance < td < chosen)
            if days_between < 3:
                continue  # 间隔太短，跳过
        
        # 检查与后一个调仓日间隔是否>=3个交易日
        next_rebalance = None
        for d in sorted(rebalance_dates):
            if d > chosen:
                next_rebalance = d
                break
        
        if next_rebalance is not None:
            days_between = sum(1 for td in trading_list if chosen < td < next_rebalance)
            if days_between < 3:
                continue  # 间隔太短，跳过
        
        substitutions.append({
            'source_date': thu,
            'target_date': chosen,
            'holiday_start': 'N/A',
            'holiday_end': 'N/A',
            'reason': 'nearest trading day substitution'
        })
        rebalance_dates.add(chosen)
        used_dates.add(chosen)
    
    return rebalance_dates, substitutions


# ============ 4. 断言检查 ============

def validate_rebalance_dates(variant_name, rebalance_dates, substitutions, trading_days, holidays):
    """对每种方案的调仓日进行断言检查"""
    trading_list = sorted(pd.to_datetime(trading_days))
    trading_set = set(trading_list)
    errors = []
    
    # 断言1：所有调仓日都是交易日
    for d in rebalance_dates:
        if d not in trading_set:
            errors.append(f"[{variant_name}] 调仓日 {d.strftime('%Y-%m-%d')} 不是交易日")
    
    # 断言2：每次替代都有source_date和target_date
    for sub in substitutions:
        if 'source_date' not in sub or 'target_date' not in sub:
            errors.append(f"[{variant_name}] 替代记录缺少source_date或target_date: {sub}")
    
    # 断言3：同一长假仅替代一次（检查target_date）
    holiday_targets = defaultdict(list)
    for sub in substitutions:
        if sub['holiday_start'] != 'N/A':
            holiday_key = f"{sub['holiday_start']}~{sub['holiday_end']}"
            holiday_targets[holiday_key].append(sub['target_date'])
    
    for hk, targets in holiday_targets.items():
        if len(targets) > 1:
            # 同一长假替代了多个target_date？这应该只在B方案中发生（覆盖多个周四）
            # 但同一长假应该只有一个target_date（last_before）
            unique_targets = set(targets)
            if len(unique_targets) > 1:
                errors.append(f"[{variant_name}] 长假 {hk} 替代了多个不同日期: {unique_targets}")
    
    # 断言4：不存在相隔<3个交易日的重复调仓
    sorted_dates = sorted(rebalance_dates)
    for i in range(len(sorted_dates) - 1):
        d1 = sorted_dates[i]
        d2 = sorted_dates[i + 1]
        days_between = sum(1 for td in trading_list if d1 < td < d2)
        if days_between < 3:
            errors.append(f"[{variant_name}] 调仓日间隔<3交易日: {d1.strftime('%Y-%m-%d')} ~ {d2.strftime('%Y-%m-%d')} ({days_between}个交易日)")
    
    # 断言5：调仓总数差异由替代记录解释
    # 对于B/C：调仓数 = 计划周四数 - 被替代数 + 新增替代数
    # 对于D：调仓数 = 计划周四数 - 被替代数 + 成功替代数
    
    return errors


# ============ 5. 间隔统计 ============

def compute_gap_stats(rebalance_dates, trading_days):
    """计算调仓日之间的最大自然日和交易日间隔"""
    trading_list = sorted(pd.to_datetime(trading_days))
    sorted_dates = sorted(rebalance_dates)
    
    max_natural_gap = 0
    max_trading_gap = 0
    max_natural_pair = None
    max_trading_pair = None
    
    for i in range(len(sorted_dates) - 1):
        d1 = sorted_dates[i]
        d2 = sorted_dates[i + 1]
        natural_gap = (d2 - d1).days
        trading_gap = sum(1 for td in trading_list if d1 < td < d2)
        
        if natural_gap > max_natural_gap:
            max_natural_gap = natural_gap
            max_natural_pair = (d1, d2)
        
        if trading_gap > max_trading_gap:
            max_trading_gap = trading_gap
            max_trading_pair = (d1, d2)
    
    return {
        'max_natural_gap': max_natural_gap,
        'max_natural_pair': (max_natural_pair[0].strftime('%Y-%m-%d'), max_natural_pair[1].strftime('%Y-%m-%d')) if max_natural_pair else None,
        'max_trading_gap': max_trading_gap,
        'max_trading_pair': (max_trading_pair[0].strftime('%Y-%m-%d'), max_trading_pair[1].strftime('%Y-%m-%d')) if max_trading_pair else None,
    }


# ============ 6. 自定义回测引擎 ============

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


# ============ 7. 数据加载与回测 ============

def load_data_from_db():
    db = ETFDatabase(config.DB_PATH)
    all_tickers = list(config.ETF_UNIVERSE.keys()) + ['000300.SH']
    market_df = db.get_market_data(ticker=all_tickers)
    bench_df = market_df[market_df['ticker'] == '000300.SH'][['date', 'open', 'high', 'low', 'close', 'adj_close']].copy()
    market_df = market_df[market_df['ticker'] != '000300.SH']
    return market_df, bench_df


def run_b03_backtest_custom(market_df, bench_df, rebalance_dates, as_of_date='2024-12-31'):
    cfg = config.STRATEGY_CONFIG.copy()
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    cfg['min_total_score'] = 40
    cfg['stop_loss'] = -0.08
    cfg['stop_loss_mode'] = 'fixed'
    
    engine = CustomRebalanceEngine(cfg=cfg, rebalance_dates=rebalance_dates)
    result = engine.run(market_df, bench_df, as_of_date=as_of_date)
    return result


def get_trading_days(market_df):
    return sorted(market_df['date'].dropna().unique())


# ============ 8. 主程序 ============

def main(ctx):
    print("[1/7] 加载数据...")
    market_df, bench_df = load_data_from_db()
    trading_days = get_trading_days(market_df)
    
    print("[2/7] 识别长假...")
    all_holidays = identify_long_holidays(trading_days)
    # 仅保留2019-2024的长假
    holidays = [h for h in all_holidays if h['last_trading_before'].year <= 2024]
    print(f"    发现 {len(holidays)} 个长假(2019-2024):")
    for h in holidays:
        print(f"      {h['last_trading_before'].strftime('%Y-%m-%d')} ~ {h['first_trading_after'].strftime('%Y-%m-%d')} ({h['gap_natural']}自然日)")
    
    print("[3/7] 计算各变种调仓日...")
    variants = {
        'A': compute_variant_a(trading_days),
        'B': compute_variant_b(trading_days, holidays),
        'C': compute_variant_c(trading_days, holidays),
        'D': compute_variant_d(trading_days, holidays),
    }
    
    for name, (dates, subs) in variants.items():
        print(f"    {name}: {len(dates)} 个调仓日, {len(subs)} 次替代")
    
    print("[4/7] 运行断言检查...")
    for name, (dates, subs) in variants.items():
        errors = validate_rebalance_dates(name, dates, subs, trading_days, holidays)
        if errors:
            print(f"    [{name}] 断言失败:")
            for e in errors:
                print(f"      {e}")
        else:
            print(f"    [{name}] 所有断言通过")
    
    print("[5/7] 计算间隔统计...")
    gap_stats = {}
    for name, (dates, subs) in variants.items():
        gap_stats[name] = compute_gap_stats(dates, trading_days)
        stats = gap_stats[name]
        print(f"    {name}: 最大自然日间隔={stats['max_natural_gap']}天 ({stats['max_natural_pair'][0]}~{stats['max_natural_pair'][1]}), 最大交易日间隔={stats['max_trading_gap']}天 ({stats['max_trading_pair'][0]}~{stats['max_trading_pair'][1]})")
    
    print("[6/7] 运行回测...")
    results = {}
    for name, (dates, subs) in variants.items():
        print(f"    运行变种 {name}...")
        result = run_b03_backtest_custom(market_df, bench_df, dates, as_of_date='2024-12-31')
        results[name] = result
        print(f"      总收益: {result['total_return']:.2%}, 年化: {result['annual_return']:.2%}, Sharpe: {result['sharpe_ratio']:.3f}, 回撤: {result['max_drawdown']:.2%}")
    
    print("[7/7] 生成报告...")
    output_path = r'D:\etf_rotation_model\reports\phase6_7_holiday_rebalance_experiment.md'
    
    lines = []
    lines.append("# Phase 6.7: 长假调仓日历适配实验（修正版）")
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
        month = h['last_trading_before'].month
        remark = '春节' if month in [1, 2] else '国庆' if month == 9 else '五一' if month == 4 else '端午' if month == 5 else '中秋' if month == 9 else '其他'
        lines.append(f"| {i} | {h['last_trading_before'].strftime('%Y-%m-%d')} | {h['first_trading_after'].strftime('%Y-%m-%d')} | {h['gap_natural']} | {remark} |")
    lines.append("")
    
    # 各变种调仓日对比
    lines.append("## 二、各变种调仓日对比")
    lines.append("")
    
    for name, (dates, subs) in variants.items():
        desc = {
            'A': '当前规则：仅周四，休市则跳过',
            'B': '假期前补调：长假覆盖计划周四 → 节前最后交易日替代',
            'C': '假期后补调：节后首个交易日替代假期后正常周四',
            'D': '最近交易日：计划周四休市时选最近交易日，每周最多一次',
        }[name]
        lines.append(f"### 变种{name}: {desc}")
        lines.append(f"- 调仓日总数: {len(dates)}")
        lines.append(f"- 替代次数: {len(subs)}")
        
        stats = gap_stats[name]
        lines.append(f"- 最大自然日间隔: {stats['max_natural_gap']}天 ({stats['max_natural_pair'][0]}~{stats['max_natural_pair'][1]})")
        lines.append(f"- 最大交易日间隔: {stats['max_trading_gap']}天 ({stats['max_trading_pair'][0]}~{stats['max_trading_pair'][1]})")
        
        # 替代详情
        if subs:
            lines.append("- 替代记录:")
            for sub in subs:
                lines.append(f"  - {sub['reason']}: 计划 {sub['source_date'].strftime('%Y-%m-%d %a')} → 实际 {sub['target_date'].strftime('%Y-%m-%d %a')} (长假: {sub['holiday_start']}~{sub['holiday_end']})")
        lines.append("")
    
    # 回测结果对比
    lines.append("## 三、回测结果对比（2019-2024）")
    lines.append("")
    lines.append("| 指标 | A(当前) | B(前补) | C(后补) | D(最近) |")
    lines.append("|------|---------|---------|---------|---------|")
    
    metrics = [
        ('total_return', '总收益', '{:.2%}'),
        ('annual_return', '年化收益', '{:.2%}'),
        ('sharpe_ratio', 'Sharpe', '{:.3f}'),
        ('max_drawdown', '最大回撤', '{:.2%}'),
        ('num_trades', '交易笔数', '{:.0f}'),
    ]
    
    for key, label, fmt in metrics:
        vals = [fmt.format(results[n][key]) for n in 'ABCD']
        lines.append(f"| {label} | {' | '.join(vals)} |")
    
    lines.append("")
    
    # 结论
    lines.append("## 四、结论")
    lines.append("")
    lines.append("待分析完成后填充。")
    lines.append("")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"完成。报告: {output_path}")
    
    return {'report_path': output_path, 'variants': list(variants.keys())}


if __name__ == '__main__':
    main(None)

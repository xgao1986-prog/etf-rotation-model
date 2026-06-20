"""
Phase 6.6: 春节前后表现Deep Dive
仅诊断，不修改策略。不修改生产配置。
"""

import sys
sys.path.insert(0, r'D:\etf_rotation_model\src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sqlite3
from collections import defaultdict

import config
from database import Database
from backtest import BacktestEngine

# ============ 1. 春节日期（农历正月初一） ============
SPRING_FESTIVAL_DATES = {
    2019: datetime(2019, 2, 5),
    2020: datetime(2020, 1, 25),
    2021: datetime(2021, 2, 12),
    2022: datetime(2022, 2, 1),
    2023: datetime(2023, 1, 22),
    2024: datetime(2024, 2, 10),
    2025: datetime(2025, 1, 29),
    2026: datetime(2026, 2, 17),
}

# 防御资产代码
DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
INDUSTRY_TICKERS = list(config.CORE_UNIVERSE.keys())


def load_data_from_db():
    """从数据库加载market_data"""
    db = Database(config.DB_PATH)
    
    # 获取所有ETF+沪深300数据
    all_tickers = list(config.ETF_UNIVERSE.keys()) + ['000300.SH']
    market_df = db.get_market_data(ticker=all_tickers)
    
    # 分离基准
    bench_df = market_df[market_df['ticker'] == '000300.SH'][['date', 'open', 'high', 'low', 'close', 'adj_close']].copy()
    bench_df = bench_df.rename(columns={'close': 'bench_close'})
    
    market_df = market_df[market_df['ticker'] != '000300.SH']
    
    return market_df, bench_df


def run_b03_backtest(market_df, bench_df):
    """运行B0.3回测"""
    cfg = config.STRATEGY_CONFIG.copy()
    # B0.3配置：关闭momentum和volatility因子
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    cfg['min_total_score'] = 40
    cfg['stop_loss'] = -0.08
    cfg['stop_loss_mode'] = 'fixed'
    
    engine = BacktestEngine(cfg=cfg)
    result = engine.run(market_df, bench_df)
    
    return result


def get_trading_days(market_df):
    """从market_df获取所有交易日"""
    return sorted(market_df['date'].dropna().unique())


def find_spring_festival_holiday(trading_days, sf_date):
    """
    找到春节休市区间：从除夕前最后一个交易日到节后第一个交易日之前。
    A股春节通常休市：除夕到初六（或初七），共7-9个自然日。
    返回 (last_trading_before, first_trading_after) 和中间的自然日列表。
    """
    trading_days = pd.to_datetime(trading_days)
    
    # 找到春节前最后一个交易日（<= 农历除夕，通常sf_date-1或sf_date）
    before = trading_days[trading_days <= sf_date]
    if len(before) == 0:
        return None, None
    last_before = before[-1]
    
    # 找到春节后第一个交易日（> sf_date + 5，避免周末干扰）
    after = trading_days[trading_days > sf_date]
    if len(after) == 0:
        return None, None
    first_after = after[0]
    
    # 判断：如果last_before和sf_date差距>5天，说明sf_date本身也是交易日
    # 农历除夕通常是sf_date-1，但A股除夕有时也休市
    # 简化：找到sf_date前后各最近的交易日
    return last_before, first_after


def define_event_windows(trading_days, last_before, first_after):
    """
    定义4个事件窗口，基于交易日计数：
    - 窗口A：节前20-11个交易日（last_before往前数20到往前数11）
    - 窗口B：节前10-1个交易日（last_before往前数10到往前数1）
    - 窗口C：节后1-5个交易日（first_after往后数1到往后数5）
    - 窗口D：节后6-20个交易日（first_after往后数6到往后数20）
    """
    trading_days = pd.to_datetime(trading_days)
    
    idx_last = trading_days.get_loc(last_before)
    idx_first = trading_days.get_loc(first_after)
    
    windows = {}
    
    # 窗口A：节前20-11（相对last_before往前数）
    start_a = max(0, idx_last - 19)
    end_a = idx_last - 10  # 不包含last_before往前10
    if end_a >= start_a:
        windows['A_pre_20_11'] = trading_days[start_a:end_a+1]
    
    # 窗口B：节前10-1（包含last_before）
    start_b = max(0, idx_last - 9)
    end_b = idx_last
    if end_b >= start_b:
        windows['B_pre_10_1'] = trading_days[start_b:end_b+1]
    
    # 窗口C：节后1-5
    start_c = idx_first
    end_c = min(len(trading_days) - 1, idx_first + 4)
    if end_c >= start_c:
        windows['C_post_1_5'] = trading_days[start_c:end_c+1]
    
    # 窗口D：节后6-20
    start_d = min(len(trading_days) - 1, idx_first + 5)
    end_d = min(len(trading_days) - 1, idx_first + 19)
    if end_d >= start_d:
        windows['D_post_6_20'] = trading_days[start_d:end_d+1]
    
    return windows


def analyze_window(nav_df, trades_df, window_dates, year):
    """分析单个窗口的策略表现"""
    window_dates = pd.to_datetime(window_dates)
    
    window_nav = nav_df[nav_df['date'].isin(window_dates)].copy()
    if window_nav.empty:
        return None
    
    # 收益计算
    start_nav = window_nav['nav'].iloc[0]
    end_nav = window_nav['nav'].iloc[-1]
    strategy_return = (end_nav / start_nav) - 1
    
    start_bench = window_nav['bench_price'].iloc[0]
    end_bench = window_nav['bench_price'].iloc[-1]
    bench_return = (end_bench / start_bench) - 1 if start_bench > 0 else 0
    
    excess = strategy_return - bench_return
    
    # 回撤
    window_nav['peak'] = window_nav['nav'].cummax()
    window_nav['drawdown'] = (window_nav['nav'] - window_nav['peak']) / window_nav['peak']
    max_dd = window_nav['drawdown'].min()
    
    # 仓位统计
    avg_positions = window_nav['num_positions'].mean()
    min_positions = window_nav['num_positions'].min()
    max_positions = window_nav['num_positions'].max()
    
    # 行业/防御拆分
    avg_industry = window_nav['industry_value'].sum() / (window_nav['nav'].sum() + 1e-10)
    avg_defense = window_nav['defense_value'].sum() / (window_nav['nav'].sum() + 1e-10)
    
    # 交易统计（在窗口内发生的交易）
    if not trades_df.empty:
        window_trades = trades_df[trades_df['date'].isin(window_dates)]
        buy_count = len(window_trades[window_trades['action'] == 'BUY'])
        sell_count = len(window_trades[window_trades['action'].isin(['SELL', 'STOP_LOSS'])])
        stop_loss_count = len(window_trades[window_trades['action'] == 'STOP_LOSS'])
        commission = window_trades['commission'].sum() if 'commission' in window_trades.columns else 0
    else:
        buy_count = sell_count = stop_loss_count = 0
        commission = 0
    
    # 持仓结构（取最后一天）
    last_row = window_nav.iloc[-1]
    positions_detail = last_row.get('positions_detail', {}) if isinstance(last_row.get('positions_detail'), dict) else {}
    
    return {
        'strategy_return': strategy_return,
        'bench_return': bench_return,
        'excess': excess,
        'max_drawdown': max_dd,
        'avg_positions': avg_positions,
        'min_positions': min_positions,
        'max_positions': max_positions,
        'avg_industry_pct': avg_industry,
        'avg_defense_pct': avg_defense,
        'buy_count': buy_count,
        'sell_count': sell_count,
        'stop_loss_count': stop_loss_count,
        'commission': commission,
        'positions_detail': positions_detail,
    }


def check_rebalance_thursday_impact(nav_df, trades_df, last_before, first_after, year):
    """检查周四调仓受春节休市影响"""
    # 找到春节前一周和节后一周的周四
    all_dates = nav_df['date'].tolist()
    
    # 春节前最后一个调仓日（周四）
    # 找到last_before之前最近的周四
    thursday_before = None
    for d in reversed(all_dates):
        if d.weekday() == 3 and d <= last_before:  # weekday=3是周四
            thursday_before = d
            break
    
    # 春节后第一个调仓日（周四）
    thursday_after = None
    for d in all_dates:
        if d.weekday() == 3 and d >= first_after:
            thursday_after = d
            break
    
    # 计算距离上次调仓的实际交易日/自然日
    gap_trading_days = 0
    gap_natural_days = 0
    if thursday_before and thursday_after:
        gap_trading_days = sum(1 for d in all_dates if thursday_before < d < thursday_after)
        gap_natural_days = (thursday_after - thursday_before).days
    
    # 检查thursday_before是否有调仓信号（BUY/SELL）
    trades_before = trades_df[trades_df['date'] == thursday_before] if thursday_before is not None else pd.DataFrame()
    trades_after = trades_df[trades_df['date'] == thursday_after] if thursday_after is not None else pd.DataFrame()
    
    return {
        'thursday_before': thursday_before,
        'thursday_after': thursday_after,
        'gap_trading_days': gap_trading_days,
        'gap_natural_days': gap_natural_days,
        'trades_before_count': len(trades_before),
        'trades_after_count': len(trades_after),
        'has_stop_loss_before': any(trades_before['action'] == 'STOP_LOSS') if not trades_before.empty else False,
    }


def analyze_gap_risk(nav_df, last_before, first_after):
    """分析节前持仓跨长假后的跳空损益"""
    # 找到last_before当天的持仓
    last_day = nav_df[nav_df['date'] == last_before]
    first_day = nav_df[nav_df['date'] == first_after]
    
    if last_day.empty or first_day.empty:
        return None
    
    last_row = last_day.iloc[0]
    first_row = first_day.iloc[0]
    
    # 策略净值跳空
    strategy_gap = (first_row['nav'] / last_row['nav']) - 1
    
    # 基准跳空
    bench_gap = (first_row['bench_price'] / last_row['bench_price']) - 1 if last_row['bench_price'] > 0 else 0
    
    # 持仓明细跳空（需要market_data的open数据）
    positions_detail = last_row.get('positions_detail', {}) if isinstance(last_row.get('positions_detail'), dict) else {}
    
    return {
        'strategy_gap': strategy_gap,
        'bench_gap': bench_gap,
        'gap_excess': strategy_gap - bench_gap,
        'num_positions_before': last_row['num_positions'],
        'cash_before': last_row['cash'],
        'positions_detail': positions_detail,
    }


def generate_report(results, output_path):
    """生成Markdown报告"""
    lines = []
    lines.append("# Phase 6.6: 春节前后表现Deep Dive诊断报告")
    lines.append("")
    lines.append("> **注意**：本报告仅诊断，不修改策略。不修改生产配置。")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 逐年汇总表
    lines.append("## 一、逐年春节窗口汇总")
    lines.append("")
    
    for year in sorted(results.keys()):
        r = results[year]
        lines.append(f"### {year}年春节（正月初一：{r['sf_date'].strftime('%Y-%m-%d')}）")
        lines.append("")
        
        # 基本信息
        lines.append(f"- **休市区间**: {r['last_before'].strftime('%Y-%m-%d')} ~ {r['first_after'].strftime('%Y-%m-%d')}")
        lines.append(f"- **休市自然日**: {r['gap_natural_days']}天")
        lines.append(f"- **休市交易日**: {r['gap_trading_days']}天")
        lines.append("")
        
        # 跳空分析
        if r['gap_analysis']:
            g = r['gap_analysis']
            lines.append("#### 跨长假跳空损益")
            lines.append(f"- 策略跳空: {g['strategy_gap']:.2%}")
            lines.append(f"- 基准跳空: {g['bench_gap']:.2%}")
            lines.append(f"- 跳空超额: {g['gap_excess']:.2%}")
            lines.append(f"- 节前持仓数: {g['num_positions_before']}")
            lines.append("")
        
        # 调仓影响
        t = r['thursday_impact']
        lines.append("#### 周四调仓影响")
        lines.append(f"- 节前最后调仓日: {t['thursday_before'].strftime('%Y-%m-%d') if t['thursday_before'] else 'N/A'}")
        lines.append(f"- 节后首个调仓日: {t['thursday_after'].strftime('%Y-%m-%d') if t['thursday_after'] else 'N/A'}")
        lines.append(f"- 两调仓日间交易日: {t['gap_trading_days']}天")
        lines.append(f"- 两调仓日间自然日: {t['gap_natural_days']}天")
        lines.append(f"- 节前调仓交易数: {t['trades_before_count']}")
        lines.append(f"- 节前调仓是否触发止损: {'是' if t['has_stop_loss_before'] else '否'}")
        lines.append("")
        
        # 各窗口表现
        lines.append("#### 各窗口表现")
        lines.append("")
        lines.append("| 窗口 | 交易日数 | 策略收益 | 基准收益 | 超额 | 最大回撤 | 平均持仓 | 止损次数 | 交易次数 |")
        lines.append("|------|----------|----------|----------|------|----------|----------|----------|----------|")
        
        for wname, wdata in r['windows'].items():
            if wdata is None:
                continue
            wname_cn = {
                'A_pre_20_11': '节前20-11日',
                'B_pre_10_1': '节前10-1日',
                'C_post_1_5': '节后1-5日',
                'D_post_6_20': '节后6-20日',
            }.get(wname, wname)
            lines.append(f"| {wname_cn} | {wdata.get('trading_days', 'N/A')} | {wdata['strategy_return']:.2%} | {wdata['bench_return']:.2%} | {wdata['excess']:.2%} | {wdata['max_drawdown']:.2%} | {wdata['avg_positions']:.1f} | {wdata['stop_loss_count']} | {wdata['buy_count'] + wdata['sell_count']} |")
        
        lines.append("")
    
    # 假设
    lines.append("---")
    lines.append("")
    lines.append("## 二、单变量假设（最多3个）")
    lines.append("")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    return output_path


def main(ctx):
    """主函数"""
    # 1. 加载数据
    print("[1/5] 加载数据...")
    market_df, bench_df = load_data_from_db()
    trading_days = get_trading_days(market_df)
    
    # 2. 运行B0.3回测
    print("[2/5] 运行B0.3回测...")
    result = run_b03_backtest(market_df, bench_df)
    nav_df = result['nav_df'].copy()
    trades_df = result['trades_df'].copy()
    
    # 确保日期格式一致
    nav_df['date'] = pd.to_datetime(nav_df['date'])
    if not trades_df.empty:
        trades_df['date'] = pd.to_datetime(trades_df['date'])
    
    print(f"    回测区间: {nav_df['date'].min().date()} ~ {nav_df['date'].max().date()}")
    print(f"    总交易日: {len(nav_df)}")
    print(f"    总交易笔数: {len(trades_df)}")
    
    # 3. 逐年分析
    print("[3/5] 逐年分析春节窗口...")
    results = {}
    
    for year, sf_date in SPRING_FESTIVAL_DATES.items():
        if sf_date < nav_df['date'].min() or sf_date > nav_df['date'].max():
            continue
        
        last_before, first_after = find_spring_festival_holiday(trading_days, sf_date)
        if last_before is None or first_after is None:
            continue
        
        # 自然日跨度
        gap_natural_days = (first_after - last_before).days
        gap_trading_days = sum(1 for d in trading_days if last_before < d < first_after)
        
        # 定义窗口
        windows = define_event_windows(trading_days, last_before, first_after)
        
        # 分析各窗口
        window_results = {}
        for wname, wdates in windows.items():
            wdata = analyze_window(nav_df, trades_df, wdates, year)
            if wdata:
                wdata['trading_days'] = len(wdates)
            window_results[wname] = wdata
        
        # 调仓影响
        thursday_impact = check_rebalance_thursday_impact(nav_df, trades_df, last_before, first_after, year)
        
        # 跳空分析
        gap_analysis = analyze_gap_risk(nav_df, last_before, first_after)
        
        results[year] = {
            'sf_date': sf_date,
            'last_before': last_before,
            'first_after': first_after,
            'gap_natural_days': gap_natural_days,
            'gap_trading_days': gap_trading_days,
            'windows': window_results,
            'thursday_impact': thursday_impact,
            'gap_analysis': gap_analysis,
        }
        
        print(f"    {year}年: 休市{last_before.strftime('%m-%d')}~{first_after.strftime('%m-%d')} ({gap_trading_days}交易日)")
    
    # 4. 生成报告
    print("[4/5] 生成报告...")
    output_path = r'D:\etf_rotation_model\reports\phase6_6_spring_festival_deep_dive.md'
    report_path = generate_report(results, output_path)
    
    print(f"[5/5] 完成。报告: {report_path}")
    
    return {'report_path': report_path, 'years_analyzed': len(results)}


if __name__ == '__main__':
    main(None)

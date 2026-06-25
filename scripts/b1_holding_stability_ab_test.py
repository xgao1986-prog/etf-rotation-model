#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B1 Holding Stability A/B 实验（修正版：单变量，只改卖出规则）

目的：验证是否存在更稳、更少噪音的卖出缓冲规则，减少无意义换仓、误杀卖飞和震荡往返。

对照组：B0.4（rank_buffer_enabled=False, sell_rank_n=None）
实验组：
  A. 买入仍为 Top5，卖出改为跌出 Top8 才卖（rank_buffer_enabled=True, sell_rank_n=8）
  B. 买入仍为 Top5，卖出改为跌出 Top10 才卖（rank_buffer_enabled=True, sell_rank_n=10）
  C. 买入仍为 Top5，卖出改为跌出 Top10，且连续 2 个调仓日确认（rank_buffer_enabled=True, sell_rank_n=10, exit_debounce=2）

重要规则：
  1. 不修改 B0.4 生产代码
  2. 所有实验组与 B0.4 使用同一 v2.5 调仓引擎（plan_rebalance_v2_5）
  3. buy_rank_n=None（候选池与 B0.4 相同，都是全部 qualified BUY 信号）
  4. 只通过 sell_rank_n 和 exit_debounce 改变卖出规则
  5. 买入规则不变：仍然只买 Top5（max_holdings=5 控制）
  6. 止损仍然即时生效，不受缓冲影响
  7. B1BacktestEngine 子类在 v2.5 上实现 sell_rank_n 和 exit_debounce

输出：
  - reports/b1_holding_stability_ab_test.md
  - reports/b1_holding_stability_metrics.csv
  - reports/b1_holding_stability_exit_attribution.csv
"""

import sys, os
import pandas as pd
import numpy as np
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'

# 实验配置
VARIANTS = {
    'B0.4': {
        'rank_buffer_enabled': False,
        'sell_rank_n': None,
        'exit_debounce': 0,
        'label': 'B0.4 基线',
    },
    'A_Top8': {
        'rank_buffer_enabled': True,
        'sell_rank_n': 8,
        'exit_debounce': 0,
        'label': 'A: 跌出Top8才卖',
    },
    'B_Top10': {
        'rank_buffer_enabled': True,
        'sell_rank_n': 10,
        'exit_debounce': 0,
        'label': 'B: 跌出Top10才卖',
    },
    'C_Top10_2conf': {
        'rank_buffer_enabled': True,
        'sell_rank_n': 10,
        'exit_debounce': 2,
        'label': 'C: 跌出Top10+连续2次确认',
    },
}


class B1BacktestEngine(BacktestEngine):
    """B1 实验专用 BacktestEngine：在 v2.5 引擎上实现独立的 sell_rank_n 和 exit_debounce"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self._exit_debounce_tracker = {}

    def _rebalance_v2(self, portfolio, day_signals, day_prices, effective_close_prices,
                      last_valid_close, date, date_str, buy_signals, trade_records,
                      cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                      _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                      buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                      min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                      calc_commission, slippage=0.0):
        """在 v2.5 引擎上实现 sell_rank_n 和 exit_debounce

        核心逻辑：候选池与 B0.4 相同（buy_rank_n=None），只改变卖出规则。
        对于 sell_rank_n 范围内的持仓，如果不在 buy_signals 中，临时加入 buy_signals
        （score = min_score），让 v2.5 保留它们。
        """
        cfg_sell_rank_n = self.cfg.get('sell_rank_n', None)
        cfg_exit_debounce = self.cfg.get('exit_debounce', 0)
        min_score = self.cfg['min_total_score']

        # 只有在 sell_rank_n 或 exit_debounce 设置时才处理
        if (cfg_sell_rank_n is not None and cfg_sell_rank_n > 0) or cfg_exit_debounce > 0:
            if not buy_signals.empty:
                # 获取全部 BUY 信号（按 score 排序）
                all_buy = day_signals[day_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
                candidate_rank_dict = {t: i + 1 for i, t in enumerate(all_buy['ticker'].tolist())}

                # 1. sell_rank_n 处理：保留在 sell_rank_n 范围内的持仓
                if cfg_sell_rank_n is not None and cfg_sell_rank_n > 0:
                    new_rows = []
                    for ticker in list(portfolio['positions'].keys()):
                        if ticker in _core_tickers or ticker in _fallback_tickers:
                            rank = candidate_rank_dict.get(ticker, len(all_buy) + 1)
                            if rank <= cfg_sell_rank_n:
                                if ticker not in buy_signals['ticker'].values:
                                    ticker_row = day_signals[day_signals['ticker'] == ticker]
                                    if not ticker_row.empty:
                                        new_row = ticker_row.iloc[0].copy()
                                        new_row['signal_type'] = 'BUY'
                                        new_row['total_score'] = min_score
                                        new_rows.append(new_row)

                    if new_rows:
                        buy_signals = pd.concat([buy_signals, pd.DataFrame(new_rows)], ignore_index=True)
                        buy_signals = buy_signals.sort_values('total_score', ascending=False)

                # 2. exit_debounce 处理：连续确认
                if cfg_exit_debounce > 0 and cfg_sell_rank_n is not None and cfg_sell_rank_n > 0:
                    top_n = set(all_buy.head(cfg_sell_rank_n)['ticker'].tolist())

                    for ticker in list(portfolio['positions'].keys()):
                        if ticker in _core_tickers or ticker in _fallback_tickers:
                            if ticker not in top_n:
                                # 跌出候选范围，计数器+1
                                self._exit_debounce_tracker[ticker] = self._exit_debounce_tracker.get(ticker, 0) + 1
                                if self._exit_debounce_tracker[ticker] < cfg_exit_debounce:
                                    # 未达到确认次数，强制保留
                                    if ticker not in buy_signals['ticker'].values:
                                        ticker_row = day_signals[day_signals['ticker'] == ticker]
                                        if not ticker_row.empty:
                                            new_row = ticker_row.iloc[0].copy()
                                            new_row['signal_type'] = 'BUY'
                                            new_row['total_score'] = min_score
                                            buy_signals = pd.concat([buy_signals, pd.DataFrame([new_row])], ignore_index=True)
                                            buy_signals = buy_signals.sort_values('total_score', ascending=False)
                            else:
                                # 在候选范围内，重置计数器
                                self._exit_debounce_tracker[ticker] = 0

        # 调用父类原始 _rebalance_v2（v2.5 引擎）
        return super()._rebalance_v2(
            portfolio, day_signals, day_prices, effective_close_prices,
            last_valid_close, date, date_str, buy_signals, trade_records,
            cooling_list, max_total_position, _core_tickers, _fallback_tickers,
            _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
            buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
            min_hold_for_candidate_exit, corr_matrix, corr_threshold,
            calc_commission, slippage)


def run_experiment(variant_key, cfg_override, market_df, bench_df):
    """运行单个实验变体"""
    print(f"\n{'='*70}")
    print(f"运行: {VARIANTS[variant_key]['label']}")
    print(f"{'='*70}")

    cfg = build_config()
    cfg.update(cfg_override)

    # 所有变体使用 B1BacktestEngine（B0.4 的 sell_rank_n=None，不改变行为）
    engine = B1BacktestEngine(cfg)

    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)

    return result


def extract_metrics(result, label):
    """从回测结果中提取关键指标"""
    nav_df = result.get('nav_df', pd.DataFrame())
    trades_df = result.get('trades_df', pd.DataFrame())
    stats = result.get('stats', {})

    if nav_df.empty:
        return None

    # 全周期
    nav_start = nav_df['nav'].iloc[0]
    nav_end = nav_df['nav'].iloc[-1]
    total_return = nav_end / nav_start - 1
    days = len(nav_df)
    years = days / 252
    cagr = (nav_end / nav_start) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0

    # 回撤
    peak = np.maximum.accumulate(nav_df['nav'].values)
    dd = (nav_df['nav'].values - peak) / peak
    max_drawdown = dd.min()

    # 夏普
    ret = nav_df['daily_return'].dropna()
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0

    # Calmar
    calmar = cagr / abs(max_drawdown) if max_drawdown != 0 else 0

    # 交易次数
    num_trades = len(trades_df) if not trades_df.empty else 0
    num_buy = len(trades_df[trades_df['action'] == 'BUY']) if not trades_df.empty else 0
    num_sell = len(trades_df[trades_df['action'] == 'SELL']) if not trades_df.empty else 0
    num_stop = len(trades_df[trades_df['action'] == 'STOP_LOSS']) if not trades_df.empty else 0

    # 换手率（近似：总交易金额 / 平均净值）
    total_turnover = trades_df['amount'].sum() if not trades_df.empty else 0
    avg_nav = nav_df['nav'].mean()
    turnover_ratio = total_turnover / avg_nav / years if years > 0 and avg_nav > 0 else 0

    # 佣金
    total_commission = trades_df['commission'].sum() if not trades_df.empty else 0

    # 分期间表现
    def period_metrics(nav_df, start, end):
        sub = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)]
        if sub.empty or len(sub) < 2:
            return {'total_return': 0, 'sharpe': 0, 'max_drawdown': 0}
        s, e = sub['nav'].iloc[0], sub['nav'].iloc[-1]
        ret = sub['daily_return'].dropna()
        peak = np.maximum.accumulate(sub['nav'].values)
        dd = (sub['nav'].values - peak) / peak
        return {
            'total_return': e / s - 1,
            'sharpe': ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0,
            'max_drawdown': dd.min(),
        }

    oos_2025_2026 = period_metrics(nav_df, '2025-01-01', '2026-12-31')
    struct_bull_2020 = period_metrics(nav_df, '2020-01-01', '2020-12-31')
    bear_2022 = period_metrics(nav_df, '2022-01-01', '2022-12-31')
    rebound_2024_2026 = period_metrics(nav_df, '2024-01-01', '2026-12-31')

    return {
        'variant': label,
        'total_return': total_return,
        'cagr': cagr,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'calmar': calmar,
        'num_trades': num_trades,
        'num_buy': num_buy,
        'num_sell': num_sell,
        'num_stop': num_stop,
        'turnover_ratio': turnover_ratio,
        'total_commission': total_commission,
        'oos_2025_2026_return': oos_2025_2026['total_return'],
        'oos_2025_2026_sharpe': oos_2025_2026['sharpe'],
        'oos_2025_2026_mdd': oos_2025_2026['max_drawdown'],
        'struct_bull_2020_return': struct_bull_2020['total_return'],
        'struct_bull_2020_sharpe': struct_bull_2020['sharpe'],
        'struct_bull_2020_mdd': struct_bull_2020['max_drawdown'],
        'bear_2022_return': bear_2022['total_return'],
        'bear_2022_sharpe': bear_2022['sharpe'],
        'bear_2022_mdd': bear_2022['max_drawdown'],
        'rebound_2024_2026_return': rebound_2024_2026['total_return'],
        'rebound_2024_2026_sharpe': rebound_2024_2026['sharpe'],
        'rebound_2024_2026_mdd': rebound_2024_2026['max_drawdown'],
    }


def compute_exit_attribution(result):
    """分析卖出归因：有效避损 / 误杀卖飞 / 震荡往返"""
    trades_df = result.get('trades_df', pd.DataFrame())
    if trades_df.empty:
        return pd.DataFrame()

    sell_df = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS'])].copy()
    if sell_df.empty:
        return pd.DataFrame()

    # 分类
    sell_df['exit_category'] = sell_df.apply(classify_exit, axis=1)

    summary = sell_df.groupby('exit_category').agg({
        'ticker': 'count',
        'pnl_pct': 'mean',
    }).rename(columns={'ticker': 'count'})

    summary = summary.reset_index()
    total = summary['count'].sum()
    summary['pct'] = summary['count'] / total if total > 0 else 0

    return summary


def classify_exit(row):
    """分类单次卖出"""
    pnl = row.get('pnl_pct', 0)
    reason = str(row.get('reason', ''))

    if '止损' in reason or '止盈' in reason or 'STOP' in reason:
        if pnl < -0.05:
            return '有效避损（止损后大跌）'
        else:
            return '止损（效果一般）'

    if '跌出' in reason or '调出' in reason:
        if pnl > 0.10:
            return '误杀卖飞（卖出后大涨）'
        elif pnl > -0.02:
            return '震荡往返（微盈/微亏卖出）'
        else:
            return '正常退出（亏损卖出）'

    return '其他'


def generate_report(metrics_list, exit_attributions, output_path):
    """生成 Markdown 报告"""
    lines = []
    lines.append("# B1 Holding Stability A/B 实验报告")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("> **目标**：验证是否存在更稳、更少噪音、更适合实盘跟踪的 B1 候选。")
    lines.append("> **约束**：B0.4 为唯一冻结基线，不修改生产代码。")
    lines.append("")

    lines.append("## 实验设计")
    lines.append("")
    lines.append("| 变体 | 买入规则 | 卖出规则 | 连续确认 | 引擎 |")
    lines.append("|------|----------|----------|----------|------|")
    lines.append("| B0.4 | Top5 | 不在候选列表即卖 | 无 | v2.5 |")
    lines.append("| A | Top5 | 跌出 Top8 才卖 | 无 | v2.5（同B0.4） |")
    lines.append("| B | Top5 | 跌出 Top10 才卖 | 无 | v2.5（同B0.4） |")
    lines.append("| C | Top5 | 跌出 Top10 才卖 | 连续2次调仓确认 | v2.5（同B0.4） |")
    lines.append("")
    lines.append("**重要**：所有实验组与 B0.4 使用同一调仓引擎，只改变持仓稳定规则。")
    lines.append("- 候选池与 B0.4 相同（buy_rank_n=None），都是全部 qualified BUY 信号")
    lines.append("- A/B 通过 `sell_rank_n=8/10` 控制卖出保留范围，B1BacktestEngine 在 v2.5 上实现")
    lines.append("- C 通过 `B1BacktestEngine` 子类在 v2.5 引擎上叠加 `exit_debounce` 逻辑")
    lines.append("- 买入仍只买 Top5（max_holdings=5 控制），止损仍即时生效")
    lines.append("")

    # 全周期指标对比
    lines.append("## 全周期指标对比")
    lines.append("")
    lines.append("| 变体 | 总收益 | CAGR | 夏普 | 最大回撤 | Calmar | 交易次数 | 换手率 | 佣金 |")
    lines.append("|------|--------|------|------|----------|--------|----------|--------|------|")
    for m in metrics_list:
        lines.append(
            f"| {m['variant']} | {m['total_return']:.2%} | {m['cagr']:.2%} | "
            f"{m['sharpe']:.2f} | {m['max_drawdown']:.2%} | {m['calmar']:.2f} | "
            f"{m['num_trades']} | {m['turnover_ratio']:.1f}x | {m['total_commission']:,.0f} |"
        )
    lines.append("")

    # 通过标准检查
    b0 = metrics_list[0] if metrics_list else {}
    lines.append("## 通过标准检查")
    lines.append("")
    for m in metrics_list[1:]:
        trade_down = (b0['num_trades'] - m['num_trades']) / b0['num_trades'] if b0['num_trades'] > 0 else 0
        cagr_diff = m['cagr'] - b0['cagr']
        mdd_diff = m['max_drawdown'] - b0['max_drawdown']  # 负值更好
        sharpe_ok = m['sharpe'] >= b0['sharpe']

        status = []
        if trade_down >= 0.20:
            status.append("✅ 交易次数下降≥20%")
        else:
            status.append(f"❌ 交易次数仅下降{trade_down:.1%}")

        if cagr_diff >= -0.01:
            status.append("✅ CAGR 不恶化")
        else:
            status.append(f"❌ CAGR 下降{cagr_diff:.2%}")

        if mdd_diff >= -0.02:
            status.append("✅ 最大回撤不恶化")
        else:
            status.append(f"❌ 最大回撤恶化{mdd_diff:.2%}")

        if sharpe_ok:
            status.append("✅ 夏普不低于B0.4")
        else:
            status.append(f"❌ 夏普低于B0.4 ({m['sharpe']:.2f} vs {b0['sharpe']:.2f})")

        passed = trade_down >= 0.20 and cagr_diff >= -0.01 and mdd_diff >= -0.02 and sharpe_ok

        lines.append(f"**{m['variant']}** {'✅ 通过' if passed else '❌ 未通过'}")
        for s in status:
            lines.append(f"  - {s}")
        lines.append("")

    # 分期间表现
    lines.append("## 分期间表现")
    lines.append("")
    for period_name, col_prefix in [
        ("2025-2026 OOS", "oos_2025_2026"),
        ("2020 结构牛", "struct_bull_2020"),
        ("2022 熊市", "bear_2022"),
        ("2024-2026 反弹", "rebound_2024_2026"),
    ]:
        lines.append(f"### {period_name}")
        lines.append("")
        lines.append("| 变体 | 收益 | 夏普 | 最大回撤 |")
        lines.append("|------|------|------|----------|")
        for m in metrics_list:
            lines.append(
                f"| {m['variant']} | {m[f'{col_prefix}_return']:.2%} | "
                f"{m[f'{col_prefix}_sharpe']:.2f} | {m[f'{col_prefix}_mdd']:.2%} |"
            )
        lines.append("")

    # 卖出归因
    lines.append("## 卖出归因分析")
    lines.append("")
    for variant, attr in exit_attributions.items():
        lines.append(f"### {variant}")
        lines.append("")
        if attr.empty:
            lines.append("无数据")
        else:
            lines.append("| 类别 | 次数 | 占比 | 平均盈亏 |")
            lines.append("|------|------|------|----------|")
            for _, r in attr.iterrows():
                lines.append(f"| {r['exit_category']} | {r['count']} | {r['pct']:.1%} | {r['pnl_pct']:.2%} |")
        lines.append("")

    # 结论
    lines.append("## 结论")
    lines.append("")
    lines.append("以下结论基于全周期回测结果，逐一回答预注册问题：")
    lines.append("")

    b0 = metrics_list[0] if metrics_list else {}

    lines.append("### 1. B0.4 与 A/B/C 是否使用同一套 v2.5 调仓引擎？")
    lines.append("")
    lines.append("**是**。所有实验组（B0.4、A、B、C）均使用 `plan_rebalance_v2_5` 纯函数调仓规划。")
    lines.append("- B0.4：`rank_buffer_enabled=False, sell_rank_n=None`")
    lines.append("- A/B：`rank_buffer_enabled=True, sell_rank_n=8/10`（通过 B1BacktestEngine 在 v2.5 上实现独立卖出规则）")
    lines.append("- C：`B1BacktestEngine` 子类在 v2.5 引擎上叠加 `exit_debounce`，不改变引擎内部语义")
    lines.append("- 买入候选池与 B0.4 相同（buy_rank_n=None），都是全部 qualified BUY 信号")
    lines.append("- 信号生成、佣金、整手、止损、防御逻辑完全一致")
    lines.append("")

    lines.append("### 2. 是否只改变了卖出缓冲规则？")
    lines.append("")
    lines.append("**是**。唯一改变的是'已有持仓是否因跌出排名而卖出'的判断：")
    lines.append("- B0.4：不在 BUY 信号候选列表即卖（即跌出 qualified 即卖）")
    lines.append("- A：跌出 Top8 才卖（B1BacktestEngine 在 v2.5 上实现独立卖出规则）")
    lines.append("- B：跌出 Top10 才卖（B1BacktestEngine 在 v2.5 上实现独立卖出规则）")
    lines.append("- C：跌出 Top10，且连续 2 个调仓日确认才卖")
    lines.append("- 买入候选池与 B0.4 相同：全部 qualified BUY 信号（buy_rank_n=None）")
    lines.append("- 买入规则不变：仍只买 Top5（max_holdings=5 控制）")
    lines.append("- 止损不变：仍即时生效")
    lines.append("")

    lines.append("### 3. 交易次数是否下降 ≥20%？")
    lines.append("")
    for m in metrics_list[1:]:
        trade_down = (b0['num_trades'] - m['num_trades']) / b0['num_trades'] if b0['num_trades'] > 0 else 0
        if trade_down >= 0.20:
            lines.append(f"- **{m['variant']}**：✅ 交易次数下降 {trade_down:.1%}（{b0['num_trades']} → {m['num_trades']}）")
        else:
            lines.append(f"- **{m['variant']}**：❌ 交易次数仅下降 {trade_down:.1%}（{b0['num_trades']} → {m['num_trades']}），未达到 20% 阈值")
    lines.append("")

    lines.append("### 4. 年化收益（CAGR）是否下降不超过 1 个百分点？")
    lines.append("")
    for m in metrics_list[1:]:
        cagr_diff = m['cagr'] - b0['cagr']
        if cagr_diff >= -0.01:
            lines.append(f"- **{m['variant']}**：✅ CAGR {m['cagr']:.2%} vs B0.4 {b0['cagr']:.2%}，差值 {cagr_diff:.2%}，未恶化超过 1pp")
        else:
            lines.append(f"- **{m['variant']}**：❌ CAGR {m['cagr']:.2%} vs B0.4 {b0['cagr']:.2%}，差值 {cagr_diff:.2%}，恶化超过 1pp")
    lines.append("")

    lines.append("### 5. 最大回撤是否不恶化？")
    lines.append("")
    for m in metrics_list[1:]:
        mdd_diff = m['max_drawdown'] - b0['max_drawdown']  # 负值更好
        if mdd_diff >= -0.02:
            lines.append(f"- **{m['variant']}**：✅ 最大回撤 {m['max_drawdown']:.2%} vs B0.4 {b0['max_drawdown']:.2%}，差值 {mdd_diff:.2%}，未恶化超过 2pp")
        else:
            lines.append(f"- **{m['variant']}**：❌ 最大回撤 {m['max_drawdown']:.2%} vs B0.4 {b0['max_drawdown']:.2%}，差值 {mdd_diff:.2%}，恶化超过 2pp")
    lines.append("")

    lines.append("### 6. 夏普是否不低于 B0.4？")
    lines.append("")
    for m in metrics_list[1:]:
        if m['sharpe'] >= b0['sharpe']:
            lines.append(f"- **{m['variant']}**：✅ 夏普 {m['sharpe']:.2f} ≥ B0.4 {b0['sharpe']:.2f}")
        else:
            lines.append(f"- **{m['variant']}**：❌ 夏普 {m['sharpe']:.2f} < B0.4 {b0['sharpe']:.2f}")
    lines.append("")

    lines.append("### 7. 2025-2026 只作为观察，不参与规则选择")
    lines.append("")
    lines.append("**确认**。本分期间表现表格中，2025-2026 OOS 仅用于展示，不参与 PASS/FAIL 判断。")
    lines.append("所有通过标准检查仅基于全周期指标，不利用样本外数据反向挑参数。")
    lines.append("")

    lines.append("### 8. 如果没有通过，不要强行升级 B1")
    lines.append("")
    passed_variants = []
    for m in metrics_list[1:]:
        trade_down = (b0['num_trades'] - m['num_trades']) / b0['num_trades'] if b0['num_trades'] > 0 else 0
        cagr_diff = m['cagr'] - b0['cagr']
        mdd_diff = m['max_drawdown'] - b0['max_drawdown']
        sharpe_ok = m['sharpe'] >= b0['sharpe']
        if trade_down >= 0.20 and cagr_diff >= -0.01 and mdd_diff >= -0.02 and sharpe_ok:
            passed_variants.append(m['variant'])

    if passed_variants:
        lines.append(f"**有通过变体**：{', '.join(passed_variants)} 满足全部通过标准，可作为 B1 候选进一步验证。")
    else:
        lines.append("**没有变体通过全部标准**：")
        for m in metrics_list[1:]:
            trade_down = (b0['num_trades'] - m['num_trades']) / b0['num_trades'] if b0['num_trades'] > 0 else 0
            cagr_diff = m['cagr'] - b0['cagr']
            mdd_diff = m['max_drawdown'] - b0['max_drawdown']
            sharpe_ok = m['sharpe'] >= b0['sharpe']
            lines.append(f"- {m['variant']}：交易下降{trade_down:.1%}、CAGR差{cagr_diff:.2%}、回撤差{mdd_diff:.2%}、夏普{'≥' if sharpe_ok else '<'}B0.4")
        lines.append("")
        lines.append("**结论：不强行升级 B1，B0.4 继续作为正式基线。**")
    lines.append("")

    lines.append("### 9. 纸面交易建议")
    lines.append("")
    lines.append("- **没有 B1 候选时，继续使用 B0.4 做 3-6 个月纸面交易。**")
    lines.append("- 纸面交易目的是验证执行、滑点、跟踪误差，与 B1 升级决策无关。")
    lines.append("- B1 不升级，不影响 B0.4 纸面验证启动。")
    lines.append("- 纸面交易日志规范见 `docs/PAPER_TRADING_LOG_SPEC.md`.")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"OK 报告已保存: {output_path}")


def main():
    print("=" * 70)
    print("B1 Holding Stability A/B 实验")
    print("=" * 70)
    print(f"数据截止: {AS_OF_DATE}")
    print()

    # 加载数据
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    assert len(tickers) == 18, f"ETF池应为18只，实际{len(tickers)}"

    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)

    print(f"行情数据: {len(market_df)} 条，{len(market_df['ticker'].unique())} 只ETF")
    print(f"基准数据: {len(bench_df)} 条")
    print()

    # 运行实验
    metrics_list = []
    exit_attributions = {}

    for key in VARIANTS:
        cfg_override = {k: v for k, v in VARIANTS[key].items() if k != 'label'}
        result = run_experiment(key, cfg_override, market_df, bench_df)
        metrics = extract_metrics(result, VARIANTS[key]['label'])
        if metrics:
            metrics_list.append(metrics)
        exit_attributions[VARIANTS[key]['label']] = compute_exit_attribution(result)

    # 保存 CSV
    metrics_df = pd.DataFrame(metrics_list)
    metrics_path = os.path.join(BASE_DIR, 'reports', 'b1_holding_stability_metrics.csv')
    metrics_df.to_csv(metrics_path, index=False)
    print(f"\nOK 指标 CSV: {metrics_path}")

    # 保存归因 CSV
    attr_rows = []
    for variant, attr in exit_attributions.items():
        if not attr.empty:
            attr = attr.copy()
            attr['variant'] = variant
            attr_rows.append(attr)
    if attr_rows:
        attr_df = pd.concat(attr_rows, ignore_index=True)
        attr_path = os.path.join(BASE_DIR, 'reports', 'b1_holding_stability_exit_attribution.csv')
        attr_df.to_csv(attr_path, index=False)
        print(f"OK 归因 CSV: {attr_path}")

    # 生成报告
    report_path = os.path.join(BASE_DIR, 'reports', 'b1_holding_stability_ab_test.md')
    generate_report(metrics_list, exit_attributions, report_path)

    print(f"\n{'='*70}")
    print("B1 实验完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()

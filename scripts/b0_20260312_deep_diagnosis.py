# -*- coding: utf-8 -*-
"""B0 2026-03-12 逐订单深度诊断"""
import sys, os, pandas as pd, numpy as np
from datetime import timedelta

sys.path.insert(0, 'D:/etf_rotation_model/src')

import config as _config_module
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from backtest import BacktestEngine
from strategy import StrategyEngine
from database import ETFDatabase
import config

DATA_END = pd.Timestamp('2026-06-05')
WARMUP_END = pd.Timestamp('2019-08-13')

ALL_CORE_TICKERS = list(config.ETF_UNIVERSE.keys())
ALL_DEFENSE_TICKERS = list(config.DEFENSE_UNIVERSE.keys())
DEFENSE_TICKERS_SET = set(ALL_DEFENSE_TICKERS)
CORE_TICKERS_SET = set(ALL_CORE_TICKERS)
ETF_NAME_MAP = {**config.ETF_UNIVERSE, **config.DEFENSE_UNIVERSE}

print("=" * 80)
print("B0 2026-03-12 逐订单深度诊断")
print("=" * 80)

# ============================================================
# 1. 运行 B0 回测（带详细日志）
# ============================================================

# 修改 BacktestEngine 以在 2026-03-12 打印详细日志
original_execute = BacktestEngine._execute_backtest

def patched_execute(self, signals_df, market_df, bench_df, corr_matrix=None, 
                     corr_threshold=0.70, excluded_tickers=None, enhanced_tickers=None,
                     unified_start=None, min_mature_count=5, performance_start=None,
                     early_exit_days=None):
    
    # 获取 run 方法中的参数
    _core_tickers = list(config.ETF_UNIVERSE.keys())
    _defense_tickers = list(config.DEFENSE_UNIVERSE.keys())
    _fallback_tickers = list(config.FALLBACK_EQUITY_UNIVERSE.keys())
    
    # 获取配置
    rank_buffer_enabled = self.cfg.get('rank_buffer_enabled', False)
    buy_rank_n = self.cfg.get('buy_rank_n', None)
    sell_rank_n = self.cfg.get('sell_rank_n', None)
    exit_debounce = self.cfg.get('exit_debounce', 0)
    min_hold_for_candidate_exit = self.cfg.get('min_hold_for_candidate_exit', 0)
    min_hold_for_stop_loss = self.cfg.get('min_hold_for_stop_loss', 0)
    same_group_max = self.cfg.get('same_group_max', 0)
    
    # ETF分组映射
    etf_group_map = {}
    if same_group_max > 0:
        for ticker, name in config.ETF_UNIVERSE.items():
            if '半导体' in name or '芯片' in name:
                etf_group_map[ticker] = '半导体芯片'
            elif '软件' in name or '云计算' in name:
                etf_group_map[ticker] = '软件云计算'
            elif '通信' in name or '5G' in name:
                etf_group_map[ticker] = '通信5G'
            elif '医药' in name or '医疗' in name or '创新药' in name:
                etf_group_map[ticker] = '医药医疗'
            elif '消费' in name or '白酒' in name or '食品饮料' in name:
                etf_group_map[ticker] = '消费白酒'
            elif '新能源' in name or '光伏' in name or '储能' in name or '碳中和' in name:
                etf_group_map[ticker] = '新能源光伏'
            elif '汽车' in name or '机器人' in name:
                etf_group_map[ticker] = '汽车机器人'
            elif '银行' in name or '券商' in name or '央企' in name:
                etf_group_map[ticker] = '金融央企'
            elif '军工' in name:
                etf_group_map[ticker] = '军工'
            elif '传媒' in name or '游戏' in name:
                etf_group_map[ticker] = '传媒游戏'
            elif '有色' in name or '黄金' in name or '油气' in name:
                etf_group_map[ticker] = '资源商品'
            elif '养殖' in name or '农业' in name or '旅游' in name:
                etf_group_map[ticker] = '农业旅游'
            elif '家电' in name:
                etf_group_map[ticker] = '家电'
            elif '港股' in name or '科技' in name:
                etf_group_map[ticker] = '港股科技'
            else:
                etf_group_map[ticker] = '其他'
    
    # 计算佣金
    def calc_commission(amount):
        return max(amount * self.cfg.get('commission_rate', 0.0003), self.cfg.get('min_commission', 5.0))
    
    # 获取信号数据
    signals = signals_df.copy()
    signals['date'] = pd.to_datetime(signals['date'])
    
    # 市场数据
    market_df_copy = market_df.copy()
    market_df_copy['date'] = pd.to_datetime(market_df_copy['date'])
    
    # 基准数据
    bench_df_copy = bench_df.copy()
    bench_df_copy['date'] = pd.to_datetime(bench_df_copy['date'])
    
    # 初始化
    initial_capital = self.cfg.get('initial_capital', 1_000_000)
    portfolio = {
        'cash': initial_capital,
        'positions': {}
    }
    
    # 回测记录
    nav_records = []
    trade_records = []
    key_dates = {'first_buy_date': None}
    cooling_list = {}  # 冷却期列表
    
    # 统一日期范围
    all_dates = sorted(set(signals['date'].unique()) & set(market_df_copy['date'].unique()))
    
    # 相关性矩阵
    if corr_matrix is None:
        corr_matrix = {}
    
    for date in all_dates:
        date_str = date.strftime('%Y-%m-%d')
        day_signals = signals[signals['date'] == date]
        day_market = market_df_copy[market_df_copy['date'] == date]
        day_bench = bench_df_copy[bench_df_copy['date'] == date]
        
        if day_market.empty or day_bench.empty:
            continue
        
        # 当日价格
        day_prices = dict(zip(day_market['ticker'], day_market['close']))
        day_close_prices = day_prices.copy()
        
        # 买入信号
        buy_signals = day_signals[day_signals['signal_type'] == 'BUY']
        
        # 大盘择时
        max_total_position = 1.0
        if 'market_signal' in day_bench.columns and not day_bench.empty:
            max_total_position = day_bench['market_signal'].iloc[0]
        
        # 计算当前净值
        current_value = portfolio['cash'] + sum(
            portfolio['positions'][t]['shares'] * day_close_prices.get(t, 0)
            for t in portfolio['positions']
        )
        
        # 防御配置比例
        _defense_allocation = 0.0
        if max_total_position < 1.0:
            _defense_allocation = 1.0 - max_total_position
        
        available_cash = portfolio['cash']
        max_new = self.cfg['max_holdings'] - len(portfolio['positions'])
        
        # ========== 诊断日志（仅2026-03-12）==========
        diag_active = (date_str == '2026-03-12')
        if diag_active:
            print(f"\n{'='*80}")
            print(f"DIAG: {date_str}")
            print(f"{'='*80}")
            print(f"  NAV (current_value) = {current_value:,.2f}")
            print(f"  Cash = {portfolio['cash']:,.2f}")
            print(f"  max_total_position = {max_total_position}")
            print(f"  _defense_allocation = {_defense_allocation:.2%}")
            print(f"  max_new = {max_new}")
            print(f"  Positions: {list(portfolio['positions'].keys())}")
            print(f"  BUY signals: {len(buy_signals)}")
            for _, row in buy_signals.iterrows():
                t = row['ticker']
                name = ETF_NAME_MAP.get(t, t)
                print(f"    {t} ({name}): score={row['total_score']:.1f}")
        
        # ========== 卖出逻辑（简化）==========
        # ... (省略卖出逻辑，直接使用原始方法)
        
        # 调用原始方法继续
        pass
    
    # 由于无法完全复刻原始方法，我们使用另一种方式：
    # 直接运行原始方法，但 hook 买入循环
    
    return original_execute(self, signals_df, market_df, bench_df, corr_matrix,
                           corr_threshold, excluded_tickers, enhanced_tickers,
                           unified_start, min_mature_count, performance_start, early_exit_days)

# 由于无法完美 hook，我们使用另一种诊断方式：
# 直接读取 v5 回测的 positions_detail 和交易记录，反推买入逻辑

print("\n[诊断方法] 直接从回测结果反推 2026-03-12 买入逻辑")
print()

# 运行回测
# ... 使用 b0_order_diagnosis.py 的方式

# 但我们需要更多信息。让我们直接运行一个修改版的 backtest.py，
# 在 2026-03-12 处打印所有关键变量。

# 实际上，最简单的方式是：检查 corr_matrix 和 same_group_max 的影响。

# 从 config 读取 corr_threshold 和 same_group_max
print("=== 配置检查 ===")
print(f"corr_threshold = {config.STRATEGY_CONFIG.get('corr_threshold', 0.70)}")
print(f"same_group_max = {config.STRATEGY_CONFIG.get('same_group_max', 0)}")
print(f"rank_buffer_enabled = {config.STRATEGY_CONFIG.get('rank_buffer_enabled', False)}")
print(f"buy_rank_n = {config.STRATEGY_CONFIG.get('buy_rank_n', None)}")
print()

# 检查冷却期配置
print(f"cooling_period = {config.STRATEGY_CONFIG.get('cooling_period', 0)}")
print(f"cooling_score_boost = {config.STRATEGY_CONFIG.get('cooling_score_boost', 0)}")
print()

print("=== 2026-03-12 候选分析 ===")
# 从诊断结果我们知道：
# 买入信号: 515880(57.1), 516160(76.6), 159865(79.2), 159697(90.0), 518880(75.8)
# 实际买入: 159865.SZ
# 保留: 518880.SH, 159697.SZ

# 假设 515880.SH 和 516160.SH 被跳过，可能原因：
# 1. 相关性去重
# 2. 同类分组限制
# 3. 冷却期
# 4. rank_buffer (如果 enabled)
# 5. day_prices 中无价格

# 让我们检查这些候选在 2026-03-12 的市场数据是否存在
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS)
market_df['date'] = pd.to_datetime(market_df['date'])

day_market = market_df[market_df['date'] == '2026-03-12']
print(f"2026-03-12 market data: {len(day_market)} tickers")
for _, row in day_market.iterrows():
    t = row['ticker']
    name = ETF_NAME_MAP.get(t, t)
    print(f"  {t} ({name}): close={row['close']:.3f}, volume={row['volume']:,.0f}")

print()

# 检查 515880.SH 和 516160.SH 是否在市场数据中
for t in ['515880.SH', '516160.SH', '159865.SZ', '159697.SZ', '518880.SH']:
    m = day_market[day_market['ticker'] == t]
    if len(m) > 0:
        print(f"{t}: found, close={m.iloc[0]['close']:.3f}")
    else:
        print(f"{t}: NOT FOUND in market data")

print()

# 检查 cooling_list
# 我们需要运行回测来获取 cooling_list
print("=== 运行简化回测以获取 cooling_list ===")

bt = BacktestEngine(cfg=config.STRATEGY_CONFIG.copy(), s1_mode=False)

# 由于无法直接 hook，我们使用另一种方法：
# 运行回测，然后在结果中查找 2026-03-12 之前的交易

bench_df = db.get_market_data(ticker=config.BENCHMARK)
bench_df['date'] = pd.to_datetime(bench_df['date'])
market_df = market_df[market_df['date'] <= DATA_END].copy()
bench_df = bench_df[bench_df['date'] <= DATA_END].copy()

bt = BacktestEngine(cfg=config.STRATEGY_CONFIG.copy(), s1_mode=False)
result = bt.run(market_df, bench_df)

# 检查交易记录中 515880.SH 和 516160.SH 的卖出历史
trades = result['trades_df']
for t in ['515880.SH', '516160.SH']:
    t_trades = trades[trades['ticker'] == t]
    print(f"\n{t} trade history:")
    for _, row in t_trades.iterrows():
        print(f"  {row['date']}: {row['action']} {row['shares']} @ {row['price']:.3f}, reason={row['reason']}")

# 检查 2026-03-12 前5天是否有卖出
print("\n=== 2026-03-12 前5天交易 ===")
pre_trades = trades[trades['date'] < '2026-03-12']
pre_trades = pre_trades[pre_trades['date'] >= '2026-03-05']
for _, row in pre_trades.iterrows():
    print(f"  {row['date']}: {row['action']} {row['ticker']} {row['shares']} @ {row['price']:.3f}")

print("\n诊断完成")

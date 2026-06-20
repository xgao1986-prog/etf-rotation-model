import sys, json, pandas as pd
import numpy as np
from datetime import datetime
sys.path.insert(0, 'src')
from database import ETFDatabase
from config import BENCHMARK, ETF_UNIVERSE, CORE_UNIVERSE, ALL_TRADABLE_ETFS, BENCHMARK_CODE
from backtest import BacktestEngine
from config import STRATEGY_CONFIG, FALLBACK_EQUITY_CONFIG, DEFENSE_CONFIG, TRADING_RULES_CONFIG

db = ETFDatabase()
all_tickers = list(ALL_TRADABLE_ETFS.keys())
market_df = db.get_market_data(ticker=all_tickers)

# 1. 各期板块表现记录（事实，非策略选择）
print("=" * 80)
print("1. 各期板块表现事实（Top 5 / Bottom 5）")
print("=" * 80)

# 按季度计算各ETF的收益率
market_df['date'] = pd.to_datetime(market_df['date'])
market_df['quarter'] = market_df['date'].dt.to_period('Q')

quarterly_returns = []
for ticker in CORE_UNIVERSE.keys():
    t_df = market_df[market_df['ticker'] == ticker].sort_values('date')
    if len(t_df) < 2:
        continue
    # 按季度计算收益率
    t_df['quarter'] = t_df['date'].dt.to_period('Q')
    q_data = t_df.groupby('quarter').agg({
        'close': ['first', 'last']
    }).reset_index()
    q_data.columns = ['quarter', 'open', 'close']
    q_data['return'] = (q_data['close'] / q_data['open'] - 1)
    q_data['ticker'] = ticker
    q_data['name'] = CORE_UNIVERSE.get(ticker, ticker)
    quarterly_returns.append(q_data)

q_returns = pd.concat(quarterly_returns, ignore_index=True)

# 记录每个季度表现最好和最差的ETF
for q in sorted(q_returns['quarter'].unique())[:12]:  # 最近12个季度
    q_data = q_returns[q_returns['quarter'] == q].sort_values('return', ascending=False)
    print(f"\n{q}:")
    print(f"  Top 3: {q_data.head(3)[['name', 'return']].to_dict('records')}")
    print(f"  Bottom 3: {q_data.tail(3)[['name', 'return']].to_dict('records')}")

# 保存到文件
with open('reports/sector_performance_quarterly.json', 'w', encoding='utf-8') as f:
    records = []
    for q in sorted(q_returns['quarter'].unique()):
        q_data = q_returns[q_returns['quarter'] == q].sort_values('return', ascending=False)
        records.append({
            'quarter': str(q),
            'top_5': q_data.head(5)[['ticker', 'name', 'return']].to_dict('records'),
            'bottom_5': q_data.tail(5)[['ticker', 'name', 'return']].to_dict('records'),
        })
    json.dump(records, f, ensure_ascii=False, indent=2)
print(f"\n季度板块表现已保存: reports/sector_performance_quarterly.json")

# 2. 加载回测交易记录，分析我们选择了什么
print("\n" + "=" * 80)
print("2. 策略选择 vs 板块表现")
print("=" * 80)

trades = pd.read_csv('reports/baseline_trades.csv')
trades['date'] = pd.to_datetime(trades['date'])
trades['quarter'] = trades['date'].dt.to_period('Q')

buys = trades[trades['action'] == 'BUY'].copy()

# 合并买入和季度收益，看是否选对了
buys_with_return = buys.merge(
    q_returns[['quarter', 'ticker', 'return']], 
    on=['quarter', 'ticker'], 
    how='left'
)

# 分析每个季度买入的ETF后续表现
print("\n各季度买入的ETF及后续季度表现:")
for q in sorted(buys_with_return['quarter'].unique())[:12]:
    q_buys = buys_with_return[buys_with_return['quarter'] == q]
    if len(q_buys) == 0:
        continue
    print(f"\n{q}: 买入{len(q_buys)}次")
    for _, row in q_buys.iterrows():
        ticker = row['ticker']
        name = CORE_UNIVERSE.get(ticker, ticker)
        ret = row['return']
        if pd.notna(ret):
            rank_info = q_returns[(q_returns['quarter'] == q) & (q_returns['ticker'] == ticker)]
            if len(rank_info) > 0:
                rank = (q_returns[q_returns['quarter'] == q]['return'] > rank_info.iloc[0]['return']).sum() + 1
                total = len(q_returns[q_returns['quarter'] == q])
                print(f"  {name}({ticker}): 季度收益 {ret:.1%}, 排名 {rank}/{total}")

# 3. 表现不好的时间归因分析
print("\n" + "=" * 80)
print("3. 表现不好时间段归因分析")
print("=" * 80)

nav = pd.read_csv('reports/baseline_nav.csv')
nav['date'] = pd.to_datetime(nav['date'])
nav['daily_return'] = nav['nav'].pct_change()

# 计算滚动回撤
nav['peak'] = nav['nav'].cummax()
nav['drawdown'] = (nav['nav'] / nav['peak'] - 1)

# 找出最大回撤期间
max_dd_idx = nav['drawdown'].idxmin()
max_dd_date = nav.loc[max_dd_idx, 'date']
max_dd_start = nav[nav['date'] <= max_dd_date]['drawdown'].idxmax()  # 回撤开始
max_dd_start_date = nav.loc[max_dd_start, 'date']

print(f"\n最大回撤期间: {max_dd_start_date.strftime('%Y-%m-%d')} ~ {max_dd_date.strftime('%Y-%m-%d')}")
print(f"最大回撤幅度: {nav.loc[max_dd_idx, 'drawdown']:.2%}")

# 分析该期间的持仓和交易
dd_trades = trades[(trades['date'] >= max_dd_start_date) & (trades['date'] <= max_dd_date)]
print(f"该期间交易: {len(dd_trades)}笔")
for _, row in dd_trades.iterrows():
    print(f"  {row['date'].strftime('%Y-%m-%d')} {row['action']} {row['ticker']} @ {row['price']:.2f} ({row.get('reason', 'N/A')})")

# 分析胜率最低的时间段
nav['month'] = nav['date'].dt.to_period('M')
monthly = nav.groupby('month').agg({
    'daily_return': lambda x: (x + 1).prod() - 1,
    'drawdown': 'min'
}).reset_index()
monthly.columns = ['month', 'monthly_return', 'max_drawdown']

worst_months = monthly.nsmallest(5, 'monthly_return')
print(f"\n表现最差的5个月:")
for _, row in worst_months.iterrows():
    m = row['month']
    print(f"\n{m}: 月收益 {row['monthly_return']:.2%}, 月内最大回撤 {row['max_drawdown']:.2%}")
    # 该月持仓
    m_trades = trades[(trades['date'] >= pd.Period(m).start_time) & (trades['date'] <= pd.Period(m).end_time)]
    m_buys = m_trades[m_trades['action'] == 'BUY']
    print(f"  该月买入: {m_buys['ticker'].tolist()}")
    m_sells = m_trades[m_trades['action'] == 'SELL']
    print(f"  该月卖出: {m_sells['ticker'].tolist()}")

# 4. 调仓日因子验证（周五 vs 其他日期）
print("\n" + "=" * 80)
print("4. 调仓日因子验证")
print("=" * 80)

# 获取各交易日的收益率（买入后持有到下一个调仓日）
# 简化：计算不同调仓日的夏普比率
rebalance_results = {}
for weekday in range(5):  # 0=周一到4=周五
    cfg_test = STRATEGY_CONFIG.copy()
    cfg_test.update(FALLBACK_EQUITY_CONFIG)
    cfg_test.update(DEFENSE_CONFIG)
    cfg_test.update(TRADING_RULES_CONFIG)
    cfg_test['rebalance_weekday'] = weekday
    
    engine = BacktestEngine(cfg=cfg_test)
    result = engine.run(market_df, db.get_market_data(ticker=BENCHMARK))
    
    day_name = ['周一', '周二', '周三', '周四', '周五'][weekday]
    rebalance_results[day_name] = {
        'total_return': result['total_return'],
        'sharpe': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
    }
    print(f"{day_name}: 收益 {result['total_return']:.2%}, 夏普 {result['sharpe_ratio']:.2f}, 回撤 {result['max_drawdown']:.2%}, 交易 {result['num_trades']}")

# 保存调仓日验证结果
with open('reports/rebalance_weekday_test.json', 'w', encoding='utf-8') as f:
    json.dump(rebalance_results, f, ensure_ascii=False, indent=2)
print(f"\n调仓日验证已保存: reports/rebalance_weekday_test.json")

print("\n" + "=" * 80)
print("分析完成！")
print("=" * 80)

import sys
sys.path.insert(0, 'src')
import pandas as pd
from database import ETFDatabase
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector
from strategy import StrategyEngine
import config

db = ETFDatabase()

# 获取所有需要的ETF数据
tickers = list(config.ETF_UNIVERSE.keys()) + list(config.DEFENSE_UNIVERSE.keys())
market_dfs = []
for ticker in tickers:
    df = db.get_market_data(ticker=ticker)
    if not df.empty:
        market_dfs.append(df)

market_df = pd.concat(market_dfs, ignore_index=True) if market_dfs else pd.DataFrame()
market_df['date'] = pd.to_datetime(market_df['date'])

bench_df = db.get_market_data(ticker=config.BENCHMARK)
bench_df['date'] = pd.to_datetime(bench_df['date'])

# 运行回测
engine = BacktestEngine()
result = engine.run(market_df, bench_df)

nav_df = result['nav_df']
print('nav_df date type:', type(nav_df['date'].iloc[0]), nav_df['date'].iloc[0])
print('nav_df date dtype:', nav_df['date'].dtype)
print('nav_df date range:', nav_df['date'].min(), '~', nav_df['date'].max())
print('nav_df head:')
print(nav_df.head(3))

# 检测状态
detector = MarketRegimeDetector()
core_tickers = list(config.ETF_UNIVERSE.keys())
market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
regime_df = detector.detect_history(bench_df, market_for_breadth)

print('regime_df date type:', type(regime_df['date'].iloc[0]), regime_df['date'].iloc[0])
print('regime_df date dtype:', regime_df['date'].dtype)
print('regime_df date range:', regime_df['date'].min(), '~', regime_df['date'].max())
print('regime_df head:')
print(regime_df.head(3))

# 转换为ns
nav_df2 = nav_df.copy().sort_values('date')
nav_df2['date'] = pd.to_datetime(nav_df2['date']).astype('datetime64[ns]')

regime_df2 = regime_df.copy().sort_values('date')
regime_df2['date'] = pd.to_datetime(regime_df2['date']).astype('datetime64[ns]')

print('after conversion:')
print('nav_df2 date dtype:', nav_df2['date'].dtype)
print('regime_df2 date dtype:', regime_df2['date'].dtype)

# merge_asof
merged = pd.merge_asof(
    nav_df2, 
    regime_df2[['date', 'regime_id', 'regime_name', 'confidence']], 
    on='date', 
    direction='backward'
)

print('merged regime_id null count:', merged['regime_id'].isna().sum(), 'of', len(merged))
print('merged head:')
print(merged.head(10))
print('merged around 2019-08-15:')
print(merged[(merged['date'] >= '2019-08-10') & (merged['date'] <= '2019-08-20')])
print('merged tail:')
print(merged.tail(10))

# 检查是否有任何非NaN值
print('regime_id unique values:', merged['regime_id'].dropna().unique()[:10])
print('regime_id non-null count:', merged['regime_id'].notna().sum())

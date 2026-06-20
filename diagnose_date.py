import sys
sys.path.insert(0, 'src')
import pandas as pd
from database import ETFDatabase
from market_regime import MarketRegimeDetector
import config

db = ETFDatabase()

# 获取基准数据
bench_df = db.get_market_data(ticker=config.BENCHMARK)
print('bench_df date type:', type(bench_df['date'].iloc[0]), bench_df['date'].iloc[0])
print('bench_df date dtype:', bench_df['date'].dtype)

# 检测状态
detector = MarketRegimeDetector()
regime_df = detector.detect_history(bench_df)
print('regime_df date type:', type(regime_df['date'].iloc[0]), regime_df['date'].iloc[0])
print('regime_df date dtype:', regime_df['date'].dtype)
print('regime_df date range:', regime_df['date'].min(), '~', regime_df['date'].max())
print('regime_df columns:', regime_df.columns.tolist())
print('regime_df head:')
print(regime_df.head(3))

# 模拟nav_df
nav_df = pd.DataFrame({'date': pd.date_range('2019-06-03', '2026-06-12', freq='B')})
nav_df['date'] = pd.to_datetime(nav_df['date'])
print('nav_df date type:', type(nav_df['date'].iloc[0]), nav_df['date'].iloc[0])
print('nav_df date dtype:', nav_df['date'].dtype)

# 尝试merge_asof
merged = pd.merge_asof(nav_df, regime_df[['date', 'regime_id']], on='date', direction='backward')
print('merged regime_id null count:', merged['regime_id'].isna().sum(), 'of', len(merged))
print('merged head:')
print(merged.head(10))
print('merged around 2019-08-15:')
print(merged[(merged['date'] >= '2019-08-10') & (merged['date'] <= '2019-08-20')])

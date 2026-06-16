import sys
sys.path.insert(0, 'src')
import pandas as pd
import sqlite3
from config import DB_PATH, ETF_UNIVERSE

conn = sqlite3.connect(DB_PATH)
original_tickers = list(ETF_UNIVERSE.keys())
print('16只原始ETF数据检查：')
print('=' * 60)

for ticker in original_tickers:
    query = 'SELECT * FROM market_data WHERE ticker = ? ORDER BY date'
    df = pd.read_sql(query, conn, params=(ticker,))
    if df.empty:
        print(f'  {ticker}: 无数据！')
        continue
    
    issues = []
    if (df['close'] <= 0).any():
        issues.append(f'close<=0 ({(df["close"]<=0).sum()}次)')
    if df['close'].isna().sum() > 0:
        issues.append(f'close为NaN ({df["close"].isna().sum()}次)')
    
    df['daily_return'] = df['close'].pct_change()
    large_jumps = df[(df['daily_return'] > 0.20) | (df['daily_return'] < -0.20)]
    if len(large_jumps) > 0:
        issues.append(f'大幅跳空({len(large_jumps)}次)')
    
    adjust_type = df['adjust_type'].iloc[0] if 'adjust_type' in df.columns else 'N/A'
    
    print(f'  {ticker}: {len(df)}条, {df["date"].min()}~{df["date"].max()}, adjust_type={adjust_type}')
    if issues:
        print(f'    ⚠️ {"; ".join(issues)}')
    
    print(f'    前5天close: {df["close"].head(5).tolist()}')
    print(f'    后5天close: {df["close"].tail(5).tolist()}')
    print()

conn.close()

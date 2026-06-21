"""Fill missing 2026-06-08~2026-06-12 data using THS (TongHuaShun) web API + akshare for CSI300."""
import sys, os, re, json, requests, sqlite3, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pandas as pd
import akshare as ak
from database import ETFDatabase
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK

THS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://stockpage.10jqka.com.cn/',
}

def fetch_ths_etf(code):
    """Fetch ETF data from THS web API (forward-adjusted)."""
    url = f'https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js'
    try:
        resp = requests.get(url, headers=THS_HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        # Extract JSON from callback
        match = re.search(r'\(({.*?})\)', text)
        if not match:
            return None
        data = json.loads(match.group(1))
        raw_data = data.get('data', '')
        if not raw_data:
            return None
        rows = []
        for item in raw_data.split(';'):
            parts = item.split(',')
            if len(parts) >= 7:
                rows.append({
                    'date': parts[0],
                    'open': float(parts[1]),
                    'high': float(parts[2]),
                    'low': float(parts[3]),
                    'close': float(parts[4]),
                    'volume': int(parts[5]),
                    'amount': float(parts[6]),
                })
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"  ERROR fetching {code}: {e}")
        return None

def fetch_csi300(start_date, end_date):
    """Fetch CSI300 index data using akshare."""
    try:
        df = ak.stock_zh_index_daily(symbol='sh000300')
        df = df[pd.to_datetime(df['date']).between(start_date, end_date)]
        df['volume'] = df['volume'].astype(int)
        return df[['date', 'open', 'high', 'low', 'close', 'volume']]
    except Exception as e:
        print(f"  ERROR fetching CSI300: {e}")
        return None

def insert_data(db, ticker, df, source='THS'):
    """Insert data into SQLite database."""
    if df is None or df.empty:
        return 0
    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    inserted = 0
    for _, row in df.iterrows():
        date = str(row['date'])
        if hasattr(date, 'strftime'):
            date = date.strftime('%Y-%m-%d')
        # Check if already exists
        cursor.execute("SELECT 1 FROM market_data WHERE ticker = ? AND date = ?", (ticker, date))
        if cursor.fetchone():
            continue
        cursor.execute(
            """INSERT INTO market_data (ticker, date, open, high, low, close, volume, amount, adj_close, source, adjust_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, date, float(row['open']), float(row['high']), float(row['low']), 
             float(row['close']), int(row['volume']), float(row.get('amount', 0)),
             float(row['close']), source, 'forward')
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted

def main():
    db = ETFDatabase()
    start_date = '2026-06-08'
    end_date = '2026-06-12'
    all_tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]))
    
    # Find missing tickers
    missing = {}
    for t in all_tickers:
        df = db.get_market_data(ticker=t, start_date=start_date, end_date=end_date)
        missing[t] = 5 - len(df)
    
    print(f"{'='*60}")
    print(f"Filling missing data: {sum(1 for v in missing.values() if v > 0)} tickers")
    print(f"Date range: {start_date} ~ {end_date}")
    print(f"{'='*60}")
    
    total_inserted = 0
    
    for ticker, missing_days in missing.items():
        if missing_days <= 0:
            continue
        print(f"\n{ticker} (missing {missing_days} days):")
        
        if ticker == BENCHMARK:
            df = fetch_csi300(start_date, end_date)
            if df is not None and not df.empty:
                print(f"  Fetched {len(df)} rows from akshare")
                inserted = insert_data(db, ticker, df, source='akshare')
                print(f"  Inserted {inserted} new records")
                total_inserted += inserted
            else:
                print(f"  FAILED")
        else:
            code = ticker.split('.')[0]
            df = fetch_ths_etf(code)
            if df is not None and not df.empty:
                # Filter to target date range
                df['date'] = pd.to_datetime(df['date'])
                df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
                print(f"  Fetched {len(df)} rows from THS")
                inserted = insert_data(db, ticker, df, source='THS')
                print(f"  Inserted {inserted} new records")
                total_inserted += inserted
            else:
                print(f"  FAILED")
        
        time.sleep(0.3)
    
    print(f"\n{'='*60}")
    print(f"Total inserted: {total_inserted} records")
    print(f"{'='*60}")
    
    # Verify
    print("\n=== Verification ===")
    for t in all_tickers:
        df = db.get_market_data(ticker=t, start_date=start_date, end_date=end_date)
        status = "OK" if len(df) == 5 else f"{len(df)}/5 days"
        print(f"{t}: {status}")

if __name__ == '__main__':
    main()

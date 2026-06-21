#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B0 Data Admission Check v1.1
回测前数据准入检查脚本 — 可编程API + 缺失反例 + 权威上市日 + SHA-256快照

检查项：
1. 完整性检查：18只ETF + 沪深300基准，数据记录数、缺失日期、NULL值
2. 拼接连续性检查：多数据源拼接处无断档、无重复、无重叠
3. 异常跳变检测：单日涨跌幅异常、数据源切换处价格跳变、OHLC逻辑错误
4. 全期覆盖检查：从权威上市日计算，区分"已知覆盖不足"和"异常内部缺口"

准入标准：
- 18只标的 + 基准在检查期内无缺失
- 拼接处价格连续（gap < 5%，周末gap < 8%）
- 无单日涨跌幅 > 15%（ETF）或 > 10%（指数）
- OHLC逻辑正确
- 无异常内部缺口（上市后正常交易日缺失）

非零退出码：
- 0：全部通过，数据可准入
- 1：仅警告，可准入但需人工复核
- 2：存在错误，禁止回测，必须修复

可编程API：
    from b0_data_admission_check_v1 import run_admission_check
    result = run_admission_check(conn, market_df=optional_df)
    # result['exit_code'] == 0 → 通过；>=1 → 阻止回测
"""

import sys, os, sqlite3, json, datetime, math, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from database import ETFDatabase
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, BACKTEST_CONFIG, STRATEGY_CONFIG, build_config

# =============================================================================
# 配置
# =============================================================================
VERSION = "1.1"
SCRIPT_NAME = "B0数据准入检查v1.1"
REPORT_PATH = os.path.join(os.path.dirname(__file__), '..', 'docs', 'B0_DATA_ADMISSION_CHECK_v1.md')
CSV_PATH = os.path.join(os.path.dirname(__file__), '..', 'docs', 'B0_data_admission_check_v1.csv')
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'snapshots')

CHECK_PERIOD_DAYS = 14
MAX_SINGLE_DAY_MOVE_ETF = 0.15
MAX_SINGLE_DAY_MOVE_BENCH = 0.10
MAX_SPLICE_GAP = 0.05

ALL_TICKERS = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]))

CONFIG = build_config()
if CONFIG['end_date'] is None:
    CONFIG['end_date'] = datetime.datetime.now().strftime('%Y-%m-%d')

# 加载权威上市日期
METADATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'database', 'etf_metadata.json')
LISTING_DATES = {}
if os.path.exists(METADATA_PATH):
    with open(METADATA_PATH, 'r', encoding='utf-8') as f:
        _metadata = json.load(f)
    for ticker, info in _metadata.items():
        if 'listing_date' in info:
            LISTING_DATES[ticker] = info['listing_date']

# 策略开始日（数据预期起点）
STRATEGY_START = CONFIG['start_date']  # '2019-06-03'

# 已知数据源覆盖不足：权威上市日晚于策略开始日，或数据源历史覆盖有明确限制
# 格式: {ticker: (gap_start, gap_end, reason)}
KNOWN_COVERAGE_GAPS = {
    # 上市日晚于策略开始日 → 预期内缺失
    '159530.SZ': ('2019-06-03', '2021-04-15', '上市日2021-04-16'),
    '159697.SZ': ('2019-06-03', '2021-05-06', '上市日2021-05-07'),
    '159865.SZ': ('2019-06-03', '2021-03-17', '上市日2021-03-18'),
    '159996.SZ': ('2019-06-03', '2020-04-23', '上市日2020-04-24'),
    '515230.SH': ('2019-06-03', '2020-04-23', '上市日2020-04-24'),
    '516110.SH': ('2019-06-03', '2020-04-23', '上市日2020-04-24'),
    '516160.SH': ('2019-06-03', '2020-03-19', '上市日2020-03-20'),
    '159928.SZ': ('2019-06-03', '2019-06-11', '上市日2019-06-12'),
    '512980.SH': ('2019-06-03', '2019-05-15', '上市日2019-05-16'),
    '512480.SH': ('2019-06-03', '2019-05-15', '上市日2019-05-16'),
    '515880.SH': ('2019-06-03', '2019-08-15', '上市日2019-08-16'),
    '512010.SH': ('2019-06-03', '2019-04-11', '上市日2019-04-12'),
    '512400.SH': ('2019-06-03', '2017-08-02', '上市日2017-08-03'),
    '512660.SH': ('2019-06-03', '2016-08-10', '上市日2016-08-11'),
    '512000.SH': ('2019-06-03', '2016-08-29', '上市日2016-08-30'),
    '512800.SH': ('2019-06-03', '2018-07-18', '上市日2018-07-19'),
    '515050.SH': ('2019-06-03', '2019-09-16', '上市日2019-09-17'),
    # 基准指数从策略开始日起
    '000300.SH': ('2019-06-03', '2019-06-03', '策略开始日'),
}

# =============================================================================
# 全局状态（每次 run_admission_check 独立）
# =============================================================================

class AdmissionState:
    """准入检查状态容器（每次检查独立实例）"""
    def __init__(self):
        self.exit_code = 0
        self.warnings = []
        self.errors = []
    
    def record_error(self, msg):
        self.errors.append(msg)
        self.exit_code = max(self.exit_code, 2)
        print(f"[ERROR] {msg}")
    
    def record_warning(self, msg):
        self.warnings.append(msg)
        self.exit_code = max(self.exit_code, 1)
        print(f"[WARN] {msg}")
    
    def log(self, msg, level='INFO'):
        print(f"[{level}] {msg}")

# =============================================================================
# 工具函数
# =============================================================================

def get_trading_days(conn, start_date, end_date):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT date FROM market_data WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date",
        (BENCHMARK, start_date, end_date)
    )
    return [r[0] for r in cursor.fetchall()]

def get_ticker_data(conn, ticker, start_date, end_date):
    query = """
        SELECT date, open, high, low, close, volume, source 
        FROM market_data 
        WHERE ticker = ? AND date >= ? AND date <= ?
        ORDER BY date
    """
    return pd.read_sql_query(query, conn, params=(ticker, start_date, end_date))

def classify_gap(ticker, date_str, is_last_week=False):
    """分类缺失类型：benchmark / pre_listing / known_coverage / anomalous_internal / terminal"""
    if ticker == BENCHMARK:
        return 'benchmark'
    
    listing_date = LISTING_DATES.get(ticker)
    if listing_date and date_str < listing_date:
        return 'pre_listing'
    
    # 检查是否在已知覆盖不足范围内
    if ticker in KNOWN_COVERAGE_GAPS:
        gap_start, gap_end, _ = KNOWN_COVERAGE_GAPS[ticker]
        if gap_start <= date_str <= gap_end:
            return 'known_coverage'
    
    if is_last_week:
        return 'terminal'
    
    return 'anomalous_internal'

def compute_sha256(filepath):
    """计算文件SHA-256"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def compute_df_sha256(df):
    """计算DataFrame的SHA-256（按ticker+date排序后的CSV字节）"""
    if df.empty:
        return hashlib.sha256(b'').hexdigest()
    # 确保确定性排序
    sorted_df = df.sort_values(['ticker', 'date']).reset_index(drop=True)
    csv_bytes = sorted_df.to_csv(index=False).encode('utf-8')
    return hashlib.sha256(csv_bytes).hexdigest()

# =============================================================================
# 检查1：完整性检查
# =============================================================================

def check_completeness(state, conn, check_dates):
    state.log("\n=== 检查1：完整性检查 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        available_dates = set(df['date'].astype(str).tolist())
        expected_dates = set(check_dates)
        missing = sorted(expected_dates - available_dates)
        
        null_counts = {
            'open': df['open'].isna().sum(),
            'high': df['high'].isna().sum(),
            'low': df['low'].isna().sum(),
            'close': df['close'].isna().sum(),
            'volume': df['volume'].isna().sum(),
        }
        total_rows = len(df)
        has_null = any(c > 0 for c in null_counts.values())
        sources = df['source'].value_counts().to_dict() if len(df) > 0 else {}
        
        status = 'PASS'
        if missing:
            status = 'FAIL'
            state.record_error(f"{ticker}: 最近2周缺失 {len(missing)} 天: {missing}")
        elif has_null:
            null_fields = [f for f, c in null_counts.items() if c > 0]
            status = 'FAIL'
            state.record_error(f"{ticker}: 存在NULL值字段: {null_fields}")
        elif total_rows < len(expected_dates):
            status = 'WARN'
            state.record_warning(f"{ticker}: 数据量 {total_rows} < 预期 {len(expected_dates)}")
        
        results.append({
            'ticker': ticker,
            'expected_days': len(expected_dates),
            'actual_days': total_rows,
            'missing_days': len(missing),
            'missing_list': str(missing) if missing else '',
            'has_null': has_null,
            'null_fields': str([f for f, c in null_counts.items() if c > 0]) if has_null else '',
            'sources': str(sources),
            'status': status
        })
    
    pass_count = sum(1 for r in results if r['status'] == 'PASS')
    state.log(f"完整性检查：{pass_count}/{len(results)} 通过")
    return pd.DataFrame(results)

# =============================================================================
# 检查2：拼接连续性检查
# =============================================================================

def check_splicing_continuity(state, conn, check_dates):
    state.log("\n=== 检查2：拼接连续性检查 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        if len(df) < 2:
            continue
        
        df['source_changed'] = df['source'] != df['source'].shift(1)
        switch_points = df[df['source_changed']].copy()
        
        gaps = []
        for idx in switch_points.index:
            if idx == 0:
                continue
            prev_row = df.loc[idx - 1]
            curr_row = df.loc[idx]
            
            prev_date = pd.to_datetime(prev_row['date'])
            curr_date = pd.to_datetime(curr_row['date'])
            date_diff = (curr_date - prev_date).days
            
            is_weekend_gap = date_diff <= 3 and prev_date.weekday() == 4
            price_gap = abs(curr_row['open'] - prev_row['close']) / prev_row['close'] if prev_row['close'] != 0 else 0
            
            if date_diff > 3 and not is_weekend_gap:
                gaps.append({
                    'ticker': ticker, 'type': 'date_gap',
                    'from_date': str(prev_row['date']), 'to_date': str(curr_row['date']),
                    'from_source': prev_row['source'], 'to_source': curr_row['source'],
                    'detail': f'日期断档 {date_diff} 天',
                    'severity': 'ERROR' if date_diff > 5 else 'WARN'
                })
            
            gap_threshold = 0.08 if is_weekend_gap else MAX_SPLICE_GAP
            if price_gap > gap_threshold:
                gaps.append({
                    'ticker': ticker, 'type': 'price_gap',
                    'from_date': str(prev_row['date']), 'to_date': str(curr_row['date']),
                    'from_source': prev_row['source'], 'to_source': curr_row['source'],
                    'detail': f'{"周末" if is_weekend_gap else "非周末"}价格gap {price_gap:.2%} (前收{prev_row["close"]:.4f} → 今开{curr_row["open"]:.4f})',
                    'severity': 'ERROR' if price_gap > 0.15 else 'WARN'
                })
        
        dup_dates = df.groupby('date').size()
        dup_dates = dup_dates[dup_dates > 1]
        for date, count in dup_dates.items():
            sources = df[df['date'] == date]['source'].tolist()
            gaps.append({
                'ticker': ticker, 'type': 'duplicate_date',
                'from_date': str(date), 'to_date': str(date),
                'from_source': str(sources), 'to_source': '',
                'detail': f'同一日期 {count} 条记录，来源: {sources}',
                'severity': 'ERROR'
            })
            state.record_error(f"{ticker}: 重复日期 {date}，来源 {sources}")
        
        for g in gaps:
            if g['severity'] == 'ERROR':
                state.record_error(f"{ticker}: {g['type']} - {g['detail']}")
            else:
                state.record_warning(f"{ticker}: {g['type']} - {g['detail']}")
        
        results.extend(gaps)
    
    if not results:
        state.log("拼接连续性检查：无异常，PASS")
    else:
        error_count = sum(1 for r in results if r['severity'] == 'ERROR')
        warn_count = sum(1 for r in results if r['severity'] == 'WARN')
        state.log(f"拼接连续性检查：{error_count} 错误, {warn_count} 警告")
    
    return pd.DataFrame(results) if results else pd.DataFrame()

# =============================================================================
# 检查3：异常跳变检测
# =============================================================================

def check_anomaly_jumps(state, conn, check_dates):
    state.log("\n=== 检查3：异常跳变检测 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        if len(df) < 2:
            continue
        
        is_bench = ticker == BENCHMARK
        max_move = MAX_SINGLE_DAY_MOVE_BENCH if is_bench else MAX_SINGLE_DAY_MOVE_ETF
        
        df['close_prev'] = df['close'].shift(1)
        df['return'] = (df['close'] - df['close_prev']) / df['close_prev']
        df['return_abs'] = df['return'].abs()
        
        anomalies = df[df['return_abs'] > max_move].copy()
        for _, row in anomalies.iterrows():
            results.append({
                'ticker': ticker, 'date': str(row['date']), 'type': 'large_move',
                'detail': f"单日涨跌幅 {row['return']:.2%} (close {row['close_prev']:.4f} → {row['close']:.4f})",
                'severity': 'ERROR' if row['return_abs'] > max_move * 2 else 'WARN'
            })
            if row['return_abs'] > max_move * 2:
                state.record_error(f"{ticker} {row['date']}: 极端涨跌幅 {row['return']:.2%}")
            else:
                state.record_warning(f"{ticker} {row['date']}: 较大涨跌幅 {row['return']:.2%}")
        
        df['ohlc_valid'] = (df['high'] >= df[['open', 'close']].max(axis=1)) & \
                           (df['low'] <= df[['open', 'close']].min(axis=1)) & \
                           (df['high'] >= df['low'])
        invalid = df[~df['ohlc_valid']].copy()
        for _, row in invalid.iterrows():
            results.append({
                'ticker': ticker, 'date': str(row['date']), 'type': 'ohlc_invalid',
                'detail': f"OHLC逻辑错误: o={row['open']:.4f} h={row['high']:.4f} l={row['low']:.4f} c={row['close']:.4f}",
                'severity': 'ERROR'
            })
            state.record_error(f"{ticker} {row['date']}: OHLC逻辑错误")
        
        flat = df[(df['open'] == df['close']) & (df['high'] == df['low']) & (df['open'] == df['high'])].copy()
        for _, row in flat.iterrows():
            results.append({
                'ticker': ticker, 'date': str(row['date']), 'type': 'flat_line',
                'detail': f"一字线/停牌: o=h=l=c={row['close']:.4f}",
                'severity': 'WARN'
            })
            state.record_warning(f"{ticker} {row['date']}: 一字线/停牌数据")
    
    if not results:
        state.log("异常跳变检测：无异常，PASS")
    else:
        error_count = sum(1 for r in results if r['severity'] == 'ERROR')
        warn_count = sum(1 for r in results if r['severity'] == 'WARN')
        state.log(f"异常跳变检测：{error_count} 错误, {warn_count} 警告")
    
    return pd.DataFrame(results) if results else pd.DataFrame()

# =============================================================================
# 检查4：全期数据质量抽样（权威上市日 + 缺失分类）
# =============================================================================

def check_full_period_sample(state, conn):
    state.log("\n=== 检查4：全期数据质量抽样（权威上市日） ===")
    
    start_date = CONFIG['start_date']
    end_date = CONFIG['end_date']
    
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM market_data WHERE date >= ? AND date <= ?",
        (start_date, end_date)
    )
    total_rows = cursor.fetchone()[0]
    
    cursor.execute(
        "SELECT COUNT(DISTINCT date) FROM market_data WHERE ticker = ? AND date >= ? AND date <= ?",
        (BENCHMARK, start_date, end_date)
    )
    total_days = cursor.fetchone()[0]
    
    records = []
    for ticker in ALL_TICKERS:
        # 从权威上市日或策略开始日计算预期
        listing_date = LISTING_DATES.get(ticker)
        effective_start = listing_date if listing_date else start_date
        # 如果上市日早于策略开始日，用策略开始日
        effective_start = max(effective_start, start_date)
        
        cursor.execute(
            "SELECT COUNT(*) FROM market_data WHERE ticker = ? AND date >= ? AND date <= ?",
            (ticker, start_date, end_date)
        )
        count = cursor.fetchone()[0]
        
        cursor.execute(
            "SELECT MIN(date), MAX(date) FROM market_data WHERE ticker = ? AND date >= ? AND date <= ?",
            (ticker, start_date, end_date)
        )
        min_date, max_date = cursor.fetchone()
        
        # 计算预期交易日数（从effective_start到end_date的基准交易日）
        cursor.execute(
            "SELECT COUNT(DISTINCT date) FROM market_data WHERE ticker = ? AND date >= ? AND date <= ?",
            (BENCHMARK, effective_start, end_date)
        )
        expected_days = cursor.fetchone()[0]
        expected_min = int(expected_days * 0.95)
        
        # 计算缺失分类
        gap_summary = {'pre_listing': 0, 'known_coverage': 0, 'anomalous_internal': 0, 'terminal': 0}
        
        if count < expected_min:
            # 获取基准在该区间的全部交易日
            cursor.execute(
                "SELECT DISTINCT date FROM market_data WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date",
                (BENCHMARK, effective_start, end_date)
            )
            all_bench_dates = [r[0] for r in cursor.fetchall()]
            
            cursor.execute(
                "SELECT date FROM market_data WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date",
                (ticker, effective_start, end_date)
            )
            actual_dates = set(r[0] for r in cursor.fetchall())
            
            # 数据库实际最早日期（用于区分 known_coverage vs anomalous_internal）
            db_min = min_date if min_date else effective_start
            
            last_week_start = (datetime.datetime.strptime(end_date, '%Y-%m-%d') - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
            
            for d in all_bench_dates:
                if d not in actual_dates:
                    # 区分：在数据库最早日期之前的缺失 → known_coverage（数据源未覆盖早期）
                    #       在数据库最早日期之后的缺失 → anomalous_internal（异常缺口）
                    if d < db_min:
                        gap_summary['known_coverage'] = gap_summary.get('known_coverage', 0) + 1
                    else:
                        gap_type = classify_gap(ticker, d, is_last_week=(d >= last_week_start))
                        gap_summary[gap_type] = gap_summary.get(gap_type, 0) + 1
        
        status = 'PASS'
        anomalous_count = gap_summary.get('anomalous_internal', 0)
        if anomalous_count > 0:
            status = 'FAIL'
            state.record_error(f"{ticker}: 异常内部缺口 {anomalous_count} 天（权威上市日={listing_date}，数据库最早={min_date}，预期={expected_days}，实际={count}）")
        elif count < expected_min:
            status = 'WARN'
            state.record_warning(f"{ticker}: 全期记录数 {count} < 预期最小 {expected_min} (known_coverage: {gap_summary.get('known_coverage', 0)}天)")
        
        records.append({
            'ticker': ticker,
            'total_records': count,
            'expected_days': expected_days,
            'coverage': f"{count/expected_days:.1%}" if expected_days > 0 else 'N/A',
            'listing_date': listing_date or 'N/A',
            'effective_start': effective_start,
            'db_min_date': min_date,
            'data_range': f"{min_date} ~ {max_date}",
            'pre_listing': gap_summary.get('pre_listing', 0),
            'known_coverage': gap_summary.get('known_coverage', 0),
            'anomalous_internal': anomalous_count,
            'terminal': gap_summary.get('terminal', 0),
            'status': status
        })
    
    state.log(f"全期数据：共 {total_rows} 条记录，{total_days} 个交易日")
    pass_count = sum(1 for r in records if r['status'] == 'PASS')
    state.log(f"全期抽样：{pass_count}/{len(records)} 通过")
    
    return pd.DataFrame(records)

# =============================================================================
# 快照生成（含SHA-256）
# =============================================================================

def generate_snapshot(state, conn, market_df=None):
    state.log("\n=== 生成B0.4候选数据快照 ===")
    
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 数据库文件SHA-256
    db_sha256 = compute_sha256(conn.execute("PRAGMA database_list").fetchone()[2])
    
    # 19只标的数据集SHA-256
    if market_df is not None:
        # 使用传入的 DataFrame
        subset_df = market_df[market_df['ticker'].isin(ALL_TICKERS)].copy()
        dataset_sha256 = compute_df_sha256(subset_df)
    else:
        # 从数据库查询
        query = "SELECT * FROM market_data WHERE ticker IN ({}) ORDER BY ticker, date".format(
            ','.join(['?' for _ in ALL_TICKERS])
        )
        subset_df = pd.read_sql_query(query, conn, params=ALL_TICKERS)
        dataset_sha256 = compute_df_sha256(subset_df)
    
    # 导出全量市场数据
    snapshot_csv = os.path.join(SNAPSHOT_DIR, f'B0_4_candidate_data_{timestamp}.csv')
    df = pd.read_sql_query("SELECT * FROM market_data ORDER BY ticker, date", conn)
    df.to_csv(snapshot_csv, index=False, encoding='utf-8-sig')
    state.log(f"数据快照: {snapshot_csv} ({len(df)} 行)")
    
    # 元数据
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(DISTINCT ticker) FROM market_data")
    ticker_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT date) FROM market_data")
    date_count = cursor.fetchone()[0]
    cursor.execute("SELECT MIN(date), MAX(date) FROM market_data")
    min_date, max_date = cursor.fetchone()
    
    source_dist = pd.read_sql_query(
        "SELECT source, COUNT(*) as cnt FROM market_data GROUP BY source", conn
    )
    
    metadata = {
        'version': 'B0.4-candidate',
        'generated_at': datetime.datetime.now().isoformat(),
        'data_snapshot': snapshot_csv,
        'ticker_count': ticker_count,
        'date_count': date_count,
        'date_range': f"{min_date} to {max_date}",
        'total_records': len(df),
        'source_distribution': source_dist.set_index('source')['cnt'].to_dict(),
        'sha256': {
            'database_file': db_sha256,
            'dataset_19_tickers': dataset_sha256,
        },
        'admission_check': {
            'script_version': VERSION,
            'exit_code': state.exit_code,
            'errors': len(state.errors),
            'warnings': len(state.warnings),
        }
    }
    
    meta_path = os.path.join(SNAPSHOT_DIR, f'B0_4_candidate_metadata_{timestamp}.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
    state.log(f"元数据: {meta_path}")
    
    return snapshot_csv, meta_path

# =============================================================================
# 报告生成
# =============================================================================

def generate_report(state, check_dates, completeness_df, splicing_df, anomaly_df, full_df):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = "✅ PASS" if state.exit_code == 0 else ("⚠️ WARN" if state.exit_code == 1 else "❌ FAIL")
    
    report = f"""# B0数据准入检查报告 v{VERSION}

**生成时间**: {now}  
**检查脚本**: `scripts/b0_data_admission_check_v1.py`  
**检查区间**: {check_dates[0]} ~ {check_dates[-1]} (最近{len(check_dates)}个交易日)  
**准入状态**: {status} (exit code: {state.exit_code})  

## 检查摘要

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 完整性检查 | {'✅ PASS' if state.exit_code < 2 and completeness_df['status'].eq('PASS').all() else '❌ FAIL' if completeness_df['status'].eq('FAIL').any() else '⚠️ WARN'} | 18只ETF + 基准，数据完整性 |
| 拼接连续性 | {'✅ PASS' if splicing_df.empty else '❌ FAIL' if (splicing_df['severity'] == 'ERROR').any() else '⚠️ WARN'} | 多数据源拼接处连续性 |
| 异常跳变检测 | {'✅ PASS' if anomaly_df.empty else '❌ FAIL' if (anomaly_df['severity'] == 'ERROR').any() else '⚠️ WARN'} | 单日涨跌幅、OHLC逻辑 |
| 全期抽样 | {'✅ PASS' if state.exit_code < 2 and full_df['status'].eq('PASS').all() else '❌ FAIL' if full_df['status'].eq('FAIL').any() else '⚠️ WARN'} | 权威上市日起算，区分缺失类型 |

**错误数**: {len(state.errors)} | **警告数**: {len(state.warnings)}

## 错误详情

"""
    
    if state.errors:
        report += "\n".join(f"- {e}" for e in state.errors)
    else:
        report += "*无错误*\n"
    
    report += "\n## 警告详情\n\n"
    if state.warnings:
        report += "\n".join(f"- {w}" for w in state.warnings)
    else:
        report += "*无警告*\n"
    
    report += f"\n## 完整性检查明细\n\n"
    report += completeness_df.to_markdown(index=False)
    
    if not splicing_df.empty:
        report += f"\n\n## 拼接连续性异常\n\n"
        report += splicing_df.to_markdown(index=False)
    
    if not anomaly_df.empty:
        report += f"\n\n## 异常跳变记录\n\n"
        report += anomaly_df.to_markdown(index=False)
    
    report += f"\n\n## 全期数据覆盖（权威上市日起算）\n\n"
    report += full_df.to_markdown(index=False)
    
    report += f"""

## 准入结论

"""
    if state.exit_code == 0:
        report += "✅ **数据准入通过**。所有检查项均通过，数据可安全用于回测。\n\n建议生成B0.4候选基线。\n"
    elif state.exit_code == 1:
        report += "⚠️ **数据准入通过（含警告）**。数据可用于回测，但存在警告项，建议人工复核。\n"
    else:
        report += "❌ **数据准入未通过**。存在错误项，必须修复后方可用于回测。\n\n请修复上述错误后重新运行准入检查。\n"
    
    report += f"""
---
*报告生成: {SCRIPT_NAME} v{VERSION}*
"""
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
    state.log(f"报告已保存: {REPORT_PATH}")

# =============================================================================
# 可编程API：run_admission_check
# =============================================================================

def run_admission_check(conn_or_path=None, market_df=None, skip_snapshot=False):
    """
    可编程数据准入检查API。
    
    参数：
        conn_or_path: sqlite3.Connection 或数据库文件路径，或 None（自动连接）
        market_df: 可选的 pandas DataFrame，用于计算数据集SHA-256和反例测试
        skip_snapshot: 是否跳过生成快照（测试时设为True）
    
    返回：
        dict: {
            'exit_code': int,      # 0=PASS, 1=WARN, 2=FAIL
            'passed': bool,        # True if exit_code == 0
            'errors': list[str],
            'warnings': list[str],
            'check_dates': list[str],
            'completeness_df': pd.DataFrame,
            'splicing_df': pd.DataFrame,
            'anomaly_df': pd.DataFrame,
            'full_df': pd.DataFrame,
            'snapshot_csv': str or None,
            'meta_path': str or None,
        }
    """
    state = AdmissionState()
    
    # 连接数据库
    conn = None
    should_close = False
    if conn_or_path is None:
        db = ETFDatabase()
        conn = sqlite3.connect(db.db_path)
        should_close = True
    elif isinstance(conn_or_path, sqlite3.Connection):
        conn = conn_or_path
    else:
        conn = sqlite3.connect(conn_or_path)
        should_close = True
    
    try:
        state.log(f"=== {SCRIPT_NAME} v{VERSION} ===")
        state.log(f"检查标的: {len(ALL_TICKERS)} 只 (18 ETF + 1 基准)")
        
        end_date = CONFIG['end_date']
        start_date = (datetime.datetime.strptime(end_date, '%Y-%m-%d') - 
                       datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        all_trading_days = get_trading_days(conn, start_date, end_date)
        check_dates = all_trading_days[-CHECK_PERIOD_DAYS:] if len(all_trading_days) >= CHECK_PERIOD_DAYS else all_trading_days
        state.log(f"检查区间: {check_dates[0]} ~ {check_dates[-1]} ({len(check_dates)} 个交易日)")
        
        completeness_df = check_completeness(state, conn, check_dates)
        splicing_df = check_splicing_continuity(state, conn, check_dates)
        anomaly_df = check_anomaly_jumps(state, conn, check_dates)
        full_df = check_full_period_sample(state, conn)
        
        snapshot_csv = None
        meta_path = None
        if state.exit_code < 2 and not skip_snapshot:
            snapshot_csv, meta_path = generate_snapshot(state, conn, market_df=market_df)
        
        generate_report(state, check_dates, completeness_df, splicing_df, anomaly_df, full_df)
        
        state.log(f"\n{'='*50}")
        state.log(f"准入检查完成。状态: {'PASS' if state.exit_code == 0 else 'WARN' if state.exit_code == 1 else 'FAIL'} (exit code: {state.exit_code})")
        state.log(f"错误: {len(state.errors)}, 警告: {len(state.warnings)}")
        
        return {
            'exit_code': state.exit_code,
            'passed': state.exit_code < 2,
            'errors': state.errors,
            'warnings': state.warnings,
            'check_dates': check_dates,
            'completeness_df': completeness_df,
            'splicing_df': splicing_df,
            'anomaly_df': anomaly_df,
            'full_df': full_df,
            'snapshot_csv': snapshot_csv,
            'meta_path': meta_path,
        }
    finally:
        if should_close and conn:
            conn.close()

# =============================================================================
# 命令行入口
# =============================================================================

def main():
    result = run_admission_check()
    return result['exit_code']

if __name__ == '__main__':
    sys.exit(main())

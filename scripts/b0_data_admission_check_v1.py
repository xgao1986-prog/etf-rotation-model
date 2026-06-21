#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B0 Data Admission Check v1.0
回测前数据准入检查脚本

检查项：
1. 完整性检查：18只ETF + 沪深300基准，数据记录数、缺失日期、NULL值
2. 拼接连续性检查：多数据源拼接处无断档、无重复、无重叠
3. 异常跳变检测：单日涨跌幅异常、数据源切换处价格跳变、OHLC逻辑错误

准入标准：
- 18只标的 + 基准在检查期内无缺失
- 拼接处价格连续（gap < 5%）
- 无单日涨跌幅 > 15%（ETF）或 > 10%（指数）
- OHLC逻辑正确（high >= max(open,close), low <= min(open,close)）

输出：
- 准入检查报告（Markdown）
- 详细检查数据（CSV）
- 若通过：生成B0.4候选数据快照（CSV + metadata）

非零退出码：
- 0：全部通过，数据可准入
- 1：仅警告，可准入但需人工复核
- 2：存在错误，禁止回测，必须修复
"""

import sys, os, sqlite3, json, datetime, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from database import ETFDatabase
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, BACKTEST_CONFIG, STRATEGY_CONFIG, build_config

# =============================================================================
# 配置
# =============================================================================
VERSION = "1.0"
SCRIPT_NAME = "B0数据准入检查v1"
REPORT_PATH = os.path.join(os.path.dirname(__file__), '..', 'docs', 'B0_DATA_ADMISSION_CHECK_v1.md')
CSV_PATH = os.path.join(os.path.dirname(__file__), '..', 'docs', 'B0_data_admission_check_v1.csv')
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'snapshots')

# 检查期（最近2周 + 回测全期抽样）
CHECK_PERIOD_DAYS = 14  # 最近N个交易日
MAX_SINGLE_DAY_MOVE_ETF = 0.15   # ETF单日最大涨跌幅
MAX_SINGLE_DAY_MOVE_BENCH = 0.10 # 基准单日最大涨跌幅
MAX_SPLICE_GAP = 0.05            # 数据源拼接处最大价格gap
MAX_NULL_RATIO = 0.001           # 允许的最大NULL比例

ALL_TICKERS = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]))

# 回测配置
CONFIG = build_config()
# 如果end_date为None，使用今天
if CONFIG['end_date'] is None:
    CONFIG['end_date'] = datetime.datetime.now().strftime('%Y-%m-%d')

EXIT_CODE = 0
WARNINGS = []
ERRORS = []

# =============================================================================
# 工具函数
# =============================================================================

def log(msg, level='INFO'):
    print(f"[{level}] {msg}")

def record_error(msg):
    global EXIT_CODE
    ERRORS.append(msg)
    EXIT_CODE = max(EXIT_CODE, 2)
    log(msg, 'ERROR')

def record_warning(msg):
    global EXIT_CODE
    WARNINGS.append(msg)
    EXIT_CODE = max(EXIT_CODE, 1)
    log(msg, 'WARN')

def get_trading_days(conn, start_date, end_date):
    """获取指定区间内的所有交易日（以基准数据为准）"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT date FROM market_data WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date",
        (BENCHMARK, start_date, end_date)
    )
    return [r[0] for r in cursor.fetchall()]

def get_ticker_data(conn, ticker, start_date, end_date):
    """获取单只标的数据"""
    query = """
        SELECT date, open, high, low, close, volume, source 
        FROM market_data 
        WHERE ticker = ? AND date >= ? AND date <= ?
        ORDER BY date
    """
    return pd.read_sql_query(query, conn, params=(ticker, start_date, end_date))

# =============================================================================
# 检查1：完整性检查
# =============================================================================

def check_completeness(conn, check_dates):
    """检查最近2周内所有标的数据完整性"""
    log("\n=== 检查1：完整性检查 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        available_dates = set(df['date'].tolist())
        expected_dates = set(check_dates)
        missing = sorted(expected_dates - available_dates)
        
        # NULL检查
        null_counts = {
            'open': df['open'].isna().sum(),
            'high': df['high'].isna().sum(),
            'low': df['low'].isna().sum(),
            'close': df['close'].isna().sum(),
            'volume': df['volume'].isna().sum(),
        }
        total_rows = len(df)
        has_null = any(c > 0 for c in null_counts.values())
        
        # 来源分布
        sources = df['source'].value_counts().to_dict() if len(df) > 0 else {}
        
        status = 'PASS'
        if missing:
            status = 'FAIL'
            record_error(f"{ticker}: 最近2周缺失 {len(missing)} 天: {missing}")
        elif has_null:
            null_fields = [f for f, c in null_counts.items() if c > 0]
            status = 'FAIL'
            record_error(f"{ticker}: 存在NULL值字段: {null_fields}")
        elif total_rows < len(expected_dates):
            status = 'WARN'
            record_warning(f"{ticker}: 数据量 {total_rows} < 预期 {len(expected_dates)}")
        
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
    log(f"完整性检查：{pass_count}/{len(results)} 通过")
    return pd.DataFrame(results)

# =============================================================================
# 检查2：拼接连续性检查
# =============================================================================

def check_splicing_continuity(conn, check_dates):
    """检查多数据源拼接处的连续性"""
    log("\n=== 检查2：拼接连续性检查 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        if len(df) < 2:
            continue
        
        # 识别数据源切换点
        df['source_changed'] = df['source'] != df['source'].shift(1)
        switch_points = df[df['source_changed']].copy()
        
        gaps = []
        for idx in switch_points.index:
            if idx == 0:
                continue
            prev_row = df.loc[idx - 1]
            curr_row = df.loc[idx]
            
            # 检查日期是否连续
            prev_date = pd.to_datetime(prev_row['date'])
            curr_date = pd.to_datetime(curr_row['date'])
            date_diff = (curr_date - prev_date).days
            
            # 跳过周末跨期（周五→周一，date_diff=3）
            is_weekend_gap = date_diff <= 3 and prev_date.weekday() == 4  # Friday
            
            # 检查价格gap（仅对非周末gap检查）
            price_gap = abs(curr_row['open'] - prev_row['close']) / prev_row['close'] if prev_row['close'] != 0 else 0
            
            if date_diff > 3 and not is_weekend_gap:  # 超过3个自然日且非周末视为断档
                gaps.append({
                    'ticker': ticker,
                    'type': 'date_gap',
                    'from_date': prev_row['date'],
                    'to_date': curr_row['date'],
                    'from_source': prev_row['source'],
                    'to_source': curr_row['source'],
                    'detail': f'日期断档 {date_diff} 天',
                    'severity': 'ERROR' if date_diff > 5 else 'WARN'
                })
            
            # 价格gap检查：周末gap放宽到8%，非周末gap保持5%
            gap_threshold = 0.08 if is_weekend_gap else MAX_SPLICE_GAP
            if price_gap > gap_threshold:
                gaps.append({
                    'ticker': ticker,
                    'type': 'price_gap',
                    'from_date': prev_row['date'],
                    'to_date': curr_row['date'],
                    'from_source': prev_row['source'],
                    'to_source': curr_row['source'],
                    'detail': f'{"周末" if is_weekend_gap else "非周末"}价格gap {price_gap:.2%} (前收{prev_row["close"]:.4f} → 今开{curr_row["open"]:.4f})',
                    'severity': 'ERROR' if price_gap > 0.15 else 'WARN'
                })
        
        # 检查重复日期（同ticker同date多来源）
        dup_dates = df.groupby('date').size()
        dup_dates = dup_dates[dup_dates > 1]
        for date, count in dup_dates.items():
            sources = df[df['date'] == date]['source'].tolist()
            gaps.append({
                'ticker': ticker,
                'type': 'duplicate_date',
                'from_date': date,
                'to_date': date,
                'from_source': str(sources),
                'to_source': '',
                'detail': f'同一日期 {count} 条记录，来源: {sources}',
                'severity': 'ERROR'
            })
            record_error(f"{ticker}: 重复日期 {date}，来源 {sources}")
        
        for g in gaps:
            if g['severity'] == 'ERROR':
                record_error(f"{ticker}: {g['type']} - {g['detail']}")
            else:
                record_warning(f"{ticker}: {g['type']} - {g['detail']}")
        
        results.extend(gaps)
    
    if not results:
        log("拼接连续性检查：无异常，PASS")
    else:
        error_count = sum(1 for r in results if r['severity'] == 'ERROR')
        warn_count = sum(1 for r in results if r['severity'] == 'WARN')
        log(f"拼接连续性检查：{error_count} 错误, {warn_count} 警告")
    
    return pd.DataFrame(results) if results else pd.DataFrame()

# =============================================================================
# 检查3：异常跳变检测
# =============================================================================

def check_anomaly_jumps(conn, check_dates):
    """检测异常价格跳变和OHLC逻辑错误"""
    log("\n=== 检查3：异常跳变检测 ===")
    results = []
    
    for ticker in ALL_TICKERS:
        df = get_ticker_data(conn, ticker, check_dates[0], check_dates[-1])
        if len(df) < 2:
            continue
        
        is_bench = ticker == BENCHMARK
        max_move = MAX_SINGLE_DAY_MOVE_BENCH if is_bench else MAX_SINGLE_DAY_MOVE_ETF
        
        # 计算日收益率
        df['close_prev'] = df['close'].shift(1)
        df['return'] = (df['close'] - df['close_prev']) / df['close_prev']
        df['return_abs'] = df['return'].abs()
        
        # 检测异常涨跌幅
        anomalies = df[df['return_abs'] > max_move].copy()
        for _, row in anomalies.iterrows():
            results.append({
                'ticker': ticker,
                'date': row['date'],
                'type': 'large_move',
                'detail': f"单日涨跌幅 {row['return']:.2%} (close {row['close_prev']:.4f} → {row['close']:.4f})",
                'severity': 'ERROR' if row['return_abs'] > max_move * 2 else 'WARN'
            })
            if row['return_abs'] > max_move * 2:
                record_error(f"{ticker} {row['date']}: 极端涨跌幅 {row['return']:.2%}")
            else:
                record_warning(f"{ticker} {row['date']}: 较大涨跌幅 {row['return']:.2%}")
        
        # OHLC逻辑检查
        df['ohlc_valid'] = (df['high'] >= df[['open', 'close']].max(axis=1)) & \
                           (df['low'] <= df[['open', 'close']].min(axis=1)) & \
                           (df['high'] >= df['low'])
        invalid = df[~df['ohlc_valid']].copy()
        for _, row in invalid.iterrows():
            results.append({
                'ticker': ticker,
                'date': row['date'],
                'type': 'ohlc_invalid',
                'detail': f"OHLC逻辑错误: o={row['open']:.4f} h={row['high']:.4f} l={row['low']:.4f} c={row['close']:.4f}",
                'severity': 'ERROR'
            })
            record_error(f"{ticker} {row['date']}: OHLC逻辑错误")
        
        # 开盘=收盘=最高=最低（一字板或停牌）
        flat = df[(df['open'] == df['close']) & (df['high'] == df['low']) & (df['open'] == df['high'])].copy()
        for _, row in flat.iterrows():
            results.append({
                'ticker': ticker,
                'date': row['date'],
                'type': 'flat_line',
                'detail': f"一字线/停牌: o=h=l=c={row['close']:.4f}",
                'severity': 'WARN'
            })
            record_warning(f"{ticker} {row['date']}: 一字线/停牌数据")
    
    if not results:
        log("异常跳变检测：无异常，PASS")
    else:
        error_count = sum(1 for r in results if r['severity'] == 'ERROR')
        warn_count = sum(1 for r in results if r['severity'] == 'WARN')
        log(f"异常跳变检测：{error_count} 错误, {warn_count} 警告")
    
    return pd.DataFrame(results) if results else pd.DataFrame()

# =============================================================================
# 检查4：全期数据质量抽样
# =============================================================================

def check_full_period_sample(conn):
    """对全回测期进行抽样质量检查"""
    log("\n=== 检查4：全期数据质量抽样 ===")
    
    start_date = CONFIG['start_date']
    end_date = CONFIG['end_date']
    
    # 加载上市日期
    metadata_path = os.path.join(os.path.dirname(__file__), '..', 'database', 'etf_metadata.json')
    listing_dates = {}
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        for ticker, info in metadata.items():
            if 'listing_date' in info:
                listing_dates[ticker] = info['listing_date']
    
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
        
        # 根据实际数据范围调整预期天数（使用数据库中该标的实际min_date）
        effective_start = max(start_date, min_date) if min_date else start_date
        cursor.execute(
            "SELECT COUNT(DISTINCT date) FROM market_data WHERE ticker = ? AND date >= ? AND date <= ?",
            (BENCHMARK, effective_start, end_date)
        )
        expected_days = cursor.fetchone()[0]
        expected_min = int(expected_days * 0.95)
        
        status = 'PASS'
        if count < expected_min:
            status = 'WARN'
            record_warning(f"{ticker}: 全期记录数 {count} < 预期最小 {expected_min} (数据范围: {min_date} ~ {max_date})")
        
        records.append({
            'ticker': ticker,
            'total_records': count,
            'expected_days': expected_days,
            'coverage': f"{count/expected_days:.1%}" if expected_days > 0 else 'N/A',
            'data_range': f"{min_date} ~ {max_date}",
            'listing_date': listing_dates.get(ticker, 'N/A'),
            'status': status
        })
    
    log(f"全期数据：共 {total_rows} 条记录，{total_days} 个交易日")
    pass_count = sum(1 for r in records if r['status'] == 'PASS')
    log(f"全期抽样：{pass_count}/{len(records)} 通过")
    
    return pd.DataFrame(records)

# =============================================================================
# 快照生成
# =============================================================================

def generate_snapshot(conn):
    """生成B0.4候选数据快照"""
    log("\n=== 生成B0.4候选数据快照 ===")
    
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # 导出全量市场数据
    snapshot_csv = os.path.join(SNAPSHOT_DIR, f'B0_4_candidate_data_{timestamp}.csv')
    df = pd.read_sql_query(
        "SELECT * FROM market_data ORDER BY ticker, date",
        conn
    )
    df.to_csv(snapshot_csv, index=False, encoding='utf-8-sig')
    log(f"数据快照: {snapshot_csv} ({len(df)} 行)")
    
    # 元数据
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(DISTINCT ticker) FROM market_data")
    ticker_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT date) FROM market_data")
    date_count = cursor.fetchone()[0]
    cursor.execute("SELECT MIN(date), MAX(date) FROM market_data")
    min_date, max_date = cursor.fetchone()
    
    source_dist = pd.read_sql_query(
        "SELECT source, COUNT(*) as cnt FROM market_data GROUP BY source",
        conn
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
        'admission_check': {
            'script_version': VERSION,
            'exit_code': EXIT_CODE,
            'errors': len(ERRORS),
            'warnings': len(WARNINGS),
        }
    }
    
    meta_path = os.path.join(SNAPSHOT_DIR, f'B0_4_candidate_metadata_{timestamp}.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
    log(f"元数据: {meta_path}")
    
    return snapshot_csv, meta_path

# =============================================================================
# 报告生成
# =============================================================================

def generate_report(check_dates, completeness_df, splicing_df, anomaly_df, full_df):
    """生成Markdown报告"""
    
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = "✅ PASS" if EXIT_CODE == 0 else ("⚠️ WARN" if EXIT_CODE == 1 else "❌ FAIL")
    
    report = f"""# B0数据准入检查报告 v{VERSION}

**生成时间**: {now}  
**检查脚本**: `scripts/b0_data_admission_check_v1.py`  
**检查区间**: {check_dates[0]} ~ {check_dates[-1]} (最近{len(check_dates)}个交易日)  
**准入状态**: {status} (exit code: {EXIT_CODE})  

## 检查摘要

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 完整性检查 | {'✅ PASS' if EXIT_CODE < 2 and completeness_df['status'].eq('PASS').all() else '❌ FAIL' if completeness_df['status'].eq('FAIL').any() else '⚠️ WARN'} | 18只ETF + 基准，数据完整性 |
| 拼接连续性 | {'✅ PASS' if splicing_df.empty else '❌ FAIL' if (splicing_df['severity'] == 'ERROR').any() else '⚠️ WARN'} | 多数据源拼接处连续性 |
| 异常跳变检测 | {'✅ PASS' if anomaly_df.empty else '❌ FAIL' if (anomaly_df['severity'] == 'ERROR').any() else '⚠️ WARN'} | 单日涨跌幅、OHLC逻辑 |
| 全期抽样 | {'✅ PASS' if EXIT_CODE < 2 and full_df['status'].eq('PASS').all() else '⚠️ WARN'} | 全回测期数据质量 |

**错误数**: {len(ERRORS)} | **警告数**: {len(WARNINGS)}

## 错误详情

"""
    
    if ERRORS:
        report += "\n".join(f"- {e}" for e in ERRORS)
    else:
        report += "*无错误*\n"
    
    report += "\n## 警告详情\n\n"
    if WARNINGS:
        report += "\n".join(f"- {w}" for w in WARNINGS)
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
    
    report += f"\n\n## 全期数据覆盖\n\n"
    report += full_df.to_markdown(index=False)
    
    report += f"""

## 准入结论

"""
    if EXIT_CODE == 0:
        report += "✅ **数据准入通过**。所有检查项均通过，数据可安全用于回测。\n\n建议生成B0.4候选基线。\n"
    elif EXIT_CODE == 1:
        report += "⚠️ **数据准入通过（含警告）**。数据可用于回测，但存在警告项，建议人工复核。\n"
    else:
        report += "❌ **数据准入未通过**。存在错误项，必须修复后方可用于回测。\n\n请修复上述错误后重新运行准入检查。\n"
    
    report += f"""
---
*报告生成: {SCRIPT_NAME} v{VERSION}*
"""
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
    log(f"报告已保存: {REPORT_PATH}")

# =============================================================================
# 主函数
# =============================================================================

def main():
    log(f"=== {SCRIPT_NAME} v{VERSION} ===")
    log(f"检查标的: {len(ALL_TICKERS)} 只 (18 ETF + 1 基准)")
    
    db = ETFDatabase()
    conn = sqlite3.connect(db.db_path)
    
    try:
        # 确定检查期（最近2周的交易日）
        end_date = CONFIG['end_date']
        start_date = (datetime.datetime.strptime(end_date, '%Y-%m-%d') - 
                       datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        all_trading_days = get_trading_days(conn, start_date, end_date)
        check_dates = all_trading_days[-CHECK_PERIOD_DAYS:] if len(all_trading_days) >= CHECK_PERIOD_DAYS else all_trading_days
        log(f"检查区间: {check_dates[0]} ~ {check_dates[-1]} ({len(check_dates)} 个交易日)")
        
        # 运行四项检查
        completeness_df = check_completeness(conn, check_dates)
        splicing_df = check_splicing_continuity(conn, check_dates)
        anomaly_df = check_anomaly_jumps(conn, check_dates)
        full_df = check_full_period_sample(conn)
        
        # 生成快照（仅在通过时）
        snapshot_csv = None
        meta_path = None
        if EXIT_CODE == 0:
            snapshot_csv, meta_path = generate_snapshot(conn)
        
        # 生成报告
        generate_report(check_dates, completeness_df, splicing_df, anomaly_df, full_df)
        
        # 保存CSV
        csv_data = {
            'completeness': completeness_df,
            'splicing': splicing_df if not splicing_df.empty else None,
            'anomaly': anomaly_df if not anomaly_df.empty else None,
            'full_period': full_df
        }
        
        with open(CSV_PATH, 'w', encoding='utf-8-sig', newline='') as f:
            import csv
            writer = csv.writer(f)
            writer.writerow(['section', 'data'])
            for section, df in csv_data.items():
                if df is not None:
                    writer.writerow([section, ''])
                    df.to_csv(f, index=False)
                    writer.writerow([])
        
        log(f"\n{'='*50}")
        log(f"准入检查完成。状态: {'PASS' if EXIT_CODE == 0 else 'WARN' if EXIT_CODE == 1 else 'FAIL'} (exit code: {EXIT_CODE})")
        log(f"错误: {len(ERRORS)}, 警告: {len(WARNINGS)}")
        if snapshot_csv:
            log(f"B0.4候选快照已生成: {snapshot_csv}")
        
    finally:
        conn.close()
    
    return EXIT_CODE

if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B0数据准入自动化测试 v1.1 final

测试项：
1. test_missing_data_antipattern - 缺失反例：在内存中删除成熟ETF的正常交易日，断言准入失败
2. test_backtest_blocked_on_admission_failure - 回测入口：mock准入失败，动态调用run_baseline，断言抛出RuntimeError
3. test_pre_listing_handling - 上市前数据处理（策略自动跳过）
4. test_complete_data_backtest - 完整数据回测（B0.4候选基线，不重新运行）
5. test_admission_check_pass - 准入检查通过验证（使用可编程API）
6. test_authoritative_listing_date - 权威上市日：不使用数据库MIN(date)自动缩短
7. test_historical_gap_classification - 历史缺失分类：区分已知覆盖不足和异常内部缺口
8. test_snapshot_metadata_hashes - 快照元数据包含SHA-256字段且为64位十六进制字符串

运行：python -m pytest tests/test_b0_data_admission.py -v
"""

import sys, os, sqlite3, json, datetime, re
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))
sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))

from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, build_config
from database import ETFDatabase
from backtest import BacktestEngine
from b0_data_admission_check_v1 import run_admission_check, ALL_TICKERS, LISTING_DATES, STRATEGY_START, KNOWN_COVERAGE_GAPS


class TestB0DataAdmission:
    """B0数据准入测试套件 v1.1 final"""
    
    @classmethod
    def setup_class(cls):
        cls.all_tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]))
        cls.db = ETFDatabase()
        cls.cfg = build_config()
        cls.cfg['fallback_equity_enabled'] = False
        cls.cfg['momentum_factor_enabled'] = False
        cls.cfg['volatility_factor_enabled'] = False
        
        # 预加载完整数据
        cls.full_market_df = cls.db.get_market_data(ticker=cls.all_tickers, start_date='2019-01-01', end_date='2026-06-18')
        cls.full_bench_df = cls.db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date='2026-06-18')
    
    def test_missing_data_antipattern(self):
        """测试1：缺失反例 — 在内存中删除一个成熟ETF的正常交易日，断言准入失败"""
        mature_etf = '512000.SH'
        
        listing_date = LISTING_DATES.get(mature_etf)
        assert listing_date and listing_date < STRATEGY_START, \
            f"{mature_etf} 不是成熟ETF，上市日={listing_date}"
        
        recent_dates = self.full_market_df[
            (self.full_market_df['ticker'] == mature_etf) &
            (self.full_market_df['date'] >= '2026-06-01')
        ]['date'].unique()
        
        assert len(recent_dates) >= 1, f"{mature_etf} 在2026-06-01后无数据"
        deleted_date = str(recent_dates[-1])[:10]
        
        market_df_missing = self.full_market_df[
            ~((self.full_market_df['ticker'] == mature_etf) & 
              (self.full_market_df['date'].astype(str).str[:10] == deleted_date))
        ].copy()
        
        remaining = market_df_missing[
            (market_df_missing['ticker'] == mature_etf) &
            (market_df_missing['date'].astype(str).str[:10] == deleted_date)
        ]
        assert len(remaining) == 0, "删除失败，记录仍存在"
        
        print(f"  删除 {mature_etf} {deleted_date} 的数据，从 {len(self.full_market_df)} 行 → {len(market_df_missing)} 行")
        
        conn = sqlite3.connect(':memory:')
        market_df_missing.to_sql('market_data', conn, index=False, if_exists='replace')
        
        result = run_admission_check(conn, market_df=market_df_missing, skip_snapshot=True)
        
        assert result['exit_code'] >= 2, \
            f"缺失反例测试失败：删除 {mature_etf} {deleted_date} 后，准入检查仍通过 (exit_code={result['exit_code']})"
        
        error_msgs = ' '.join(result['errors'])
        assert mature_etf in error_msgs, f"错误信息中未包含 {mature_etf}"
        
        conn.close()
        print(f"  [PASS] 缺失反例：删除 {mature_etf} {deleted_date} 后，准入检查失败 (exit_code={result['exit_code']})")
    
    def test_backtest_blocked_on_admission_failure(self):
        """测试2：回测入口 — mock准入返回exit_code=2，动态调用run_baseline，断言抛出RuntimeError且BacktestEngine.run未被调用"""
        # 导入run_baseline
        import b0_3_baseline as b03_module
        
        # 准备mock：run_admission_check返回exit_code=2
        mock_admission_result = {
            'exit_code': 2,
            'passed': False,
            'errors': ['Mock admission failure: missing data'],
            'warnings': [],
        }
        
        # 用mock替换 b0_3_baseline 中的 run_admission_check
        with patch.object(b03_module, 'run_admission_check', return_value=mock_admission_result) as mock_check:
            # 准备配置（与b0_3_baseline一致）
            cfg = build_config()
            cfg['fallback_equity_enabled'] = False
            cfg['momentum_factor_enabled'] = False
            cfg['volatility_factor_enabled'] = False
            
            # 断言：调用run_baseline时抛出RuntimeError
            with patch.object(BacktestEngine, 'run') as mock_run:
                try:
                    b03_module.run_baseline(cfg, "test_mock")
                    assert False, "准入失败时run_baseline应抛出RuntimeError"
                except RuntimeError as e:
                    # 断言：错误信息包含"准入检查失败"
                    assert "准入检查失败" in str(e) or "admission" in str(e).lower() or "B0" in str(e), \
                        f"RuntimeError信息不包含准入相关内容: {e}"
                    
                    # 断言：BacktestEngine.run 未被调用
                    mock_run.assert_not_called()
                    
                    # 断言：run_admission_check 被调用了一次
                    mock_check.assert_called_once()
                    
                    print(f"  [PASS] 回测入口：mock准入失败 → RuntimeError，BacktestEngine.run未调用，run_admission_check被调用{mock_check.call_count}次")
    
    def test_pre_listing_handling(self):
        """测试3：上市前数据处理 - 验证策略自动跳过历史不足50天的ETF"""
        late_etf = '159530.SZ'
        
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MIN(date), COUNT(*) FROM market_data WHERE ticker = ?",
            (late_etf,)
        )
        min_date, total_count = cursor.fetchone()
        conn.close()
        
        print(f"  {late_etf}: 最早数据={min_date}, 总记录={total_count}")
        
        engine = BacktestEngine(self.cfg)
        result = engine.run(self.full_market_df, self.full_bench_df, as_of_date='2026-06-18')
        
        trades = result['trades_df']
        if not trades.empty:
            first_trade = trades[trades['ticker'] == late_etf]
            if not first_trade.empty:
                first_trade_date = first_trade['date'].min()
                
                conn = sqlite3.connect(self.db.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(DISTINCT date) FROM market_data WHERE ticker = ? AND date >= ? AND date < ?",
                    (BENCHMARK, min_date, first_trade_date)
                )
                warmup_days = cursor.fetchone()[0]
                conn.close()
                
                assert warmup_days >= 50, f"{late_etf} 首次交易仅 warmup {warmup_days} 天，策略要求>=50"
                print(f"  [PASS] 上市前处理：{late_etf} 首次交易={first_trade_date}, warmup={warmup_days}天")
            else:
                print(f"  [PASS] 上市前处理：{late_etf} 无交易记录（可能始终未满足入场条件）")
        else:
            print(f"  [PASS] 上市前处理：无交易记录")
    
    def test_complete_data_backtest(self):
        """测试4：完整数据回测 — 验证B0.4候选基线指标（不重新运行，仅验证已有结果）"""
        snapshot_dir = os.path.join(BASE_DIR, 'data', 'snapshots')
        
        metric_files = [f for f in os.listdir(snapshot_dir) if f.startswith('B0_4_candidate_metrics_')]
        assert len(metric_files) > 0, "未找到B0.4候选指标文件"
        metric_files.sort()
        latest_metrics = os.path.join(snapshot_dir, metric_files[-1])
        
        with open(latest_metrics, 'r', encoding='utf-8') as f:
            metrics = json.load(f)
        
        assert 'final_nav' in metrics
        assert 'total_return' in metrics
        assert 'sharpe_ratio' in metrics
        
        final_nav = metrics['final_nav']
        assert 2_700_000 < final_nav < 2_800_000, f"NAV {final_nav} 不在预期范围"
        
        print(f"  [PASS] 完整数据回测：NAV={final_nav:,.2f}, 收益={metrics['total_return']:.2%}, 夏普={metrics['sharpe_ratio']:.4f} (来源: {metric_files[-1]})")
    
    def test_admission_check_pass(self):
        """测试5：准入检查通过验证 — 使用可编程API
        
        v1.1要求：区分 known_coverage 和 anomalous_internal，不得全部PASS。
        正确结果：无 anomalous_internal（exit_code!=2），有 known_coverage（不全PASS）。
        """
        result = run_admission_check(skip_snapshot=True)
        
        assert result['exit_code'] != 2, \
            f"存在异常内部缺口，准入检查失败: {result['errors']}"
        
        full_df = result['full_df']
        known_total = full_df['known_coverage'].sum()
        assert known_total > 0, \
            "所有标的均为PASS，违反v1.1'不得全部PASS'要求（应有known_coverage缺失分类）"
        
        anomalous_total = full_df['anomalous_internal'].sum()
        assert anomalous_total == 0, \
            f"发现异常内部缺口 {anomalous_total} 天，必须修复"
        
        if result['exit_code'] == 1:
            assert len(result['warnings']) > 0, "exit_code=1 但无警告信息"
        
        report_path = os.path.join(BASE_DIR, 'docs', 'B0_DATA_ADMISSION_CHECK_v1.md')
        assert os.path.exists(report_path), f"准入检查报告未生成: {report_path}"
        
        assert not result['completeness_df'].empty
        assert not result['full_df'].empty
        
        print(f"  [PASS] 准入检查验证：exit_code={result['exit_code']}, "
              f"anomalous_internal=0, known_coverage={known_total}天, "
              f"无异常缺口，有已知覆盖不足（不全PASS）")
    
    def test_authoritative_listing_date(self):
        """测试6：权威上市日 — 全期覆盖从权威上市日计算，不使用数据库MIN(date)自动缩短"""
        ticker = '159530.SZ'
        
        listing_date = LISTING_DATES.get(ticker)
        assert listing_date, f"{ticker} 无权威上市日"
        
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MIN(date) FROM market_data WHERE ticker = ?",
            (ticker,)
        )
        db_min_date = cursor.fetchone()[0]
        conn.close()
        
        result = run_admission_check(skip_snapshot=True)
        full_df = result['full_df']
        
        row = full_df[full_df['ticker'] == ticker].iloc[0]
        effective_start = row['effective_start']
        
        assert effective_start == listing_date, \
            f"{ticker} 有效起点应为权威上市日 {listing_date}，实际为 {effective_start}"
        
        assert effective_start != db_min_date or listing_date == db_min_date, \
            f"{ticker} 有效起点不应自动使用数据库MIN(date)缩短"
        
        print(f"  [PASS] 权威上市日：{ticker} 使用权威上市日 {listing_date}（数据库MIN={db_min_date}）")
    
    def test_historical_gap_classification(self):
        """测试7：历史缺失分类 — 区分已知覆盖不足和异常内部缺口"""
        result = run_admission_check(skip_snapshot=True)
        full_df = result['full_df']
        
        assert 'pre_listing' in full_df.columns
        assert 'known_coverage' in full_df.columns
        assert 'anomalous_internal' in full_df.columns
        
        for ticker in ALL_TICKERS:
            listing_date = LISTING_DATES.get(ticker)
            if listing_date and listing_date > STRATEGY_START:
                row = full_df[full_df['ticker'] == ticker].iloc[0]
                known_count = row['known_coverage']
                assert known_count > 0 or row['total_records'] > 0, \
                    f"{ticker} 上市日晚于策略开始日，但 known_coverage=0 且无记录"
        
        anomalous_total = full_df['anomalous_internal'].sum()
        if anomalous_total > 0:
            print(f"  [INFO] 发现异常内部缺口共 {anomalous_total} 天")
            for _, row in full_df[full_df['anomalous_internal'] > 0].iterrows():
                print(f"    {row['ticker']}: {row['anomalous_internal']} 天")
        else:
            print(f"  [PASS] 历史缺失分类：无异常内部缺口，已知覆盖不足已正确分类")
        
        print(f"  [PASS] 历史缺失分类：已区分 known_coverage / anomalous_internal / pre_listing")
    
    def test_snapshot_metadata_hashes(self):
        """测试8：快照元数据 — 验证包含database_file和dataset_19_tickers的SHA-256，且为64位十六进制字符串"""
        # 运行准入检查，生成快照（不skip_snapshot）
        result = run_admission_check(skip_snapshot=False)
        
        # 断言：快照已生成
        assert result['meta_path'] is not None, "元数据文件未生成"
        assert os.path.exists(result['meta_path']), f"元数据文件不存在: {result['meta_path']}"
        
        # 读取元数据
        with open(result['meta_path'], 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        
        # 验证：sha256字段存在
        assert 'sha256' in metadata, "元数据中缺少sha256字段"
        sha256 = metadata['sha256']
        
        # 验证：database_file字段存在
        assert 'database_file' in sha256, "sha256中缺少database_file字段"
        db_hash = sha256['database_file']
        assert db_hash, "database_file SHA-256为空"
        
        # 验证：dataset_19_tickers字段存在
        assert 'dataset_19_tickers' in sha256, "sha256中缺少dataset_19_tickers字段"
        dataset_hash = sha256['dataset_19_tickers']
        assert dataset_hash, "dataset_19_tickers SHA-256为空"
        
        # 验证：均为64位十六进制字符串
        hex64_pattern = re.compile(r'^[0-9a-f]{64}$')
        assert hex64_pattern.match(db_hash), \
            f"database_file SHA-256不是64位十六进制: {db_hash}"
        assert hex64_pattern.match(dataset_hash), \
            f"dataset_19_tickers SHA-256不是64位十六进制: {dataset_hash}"
        
        print(f"  [PASS] 元数据哈希：database_file={db_hash[:16]}..., dataset_19_tickers={dataset_hash[:16]}...")
        print(f"  元数据文件: {result['meta_path']}")


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])

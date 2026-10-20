#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_data_fetcher.py — data fetcher fallback & iFinD CSV import tests."""

import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_fetcher import DataFetcher, import_local_ifind_csvs, update_latest_data
from database import ETFDatabase


def test_update_latest_data_returns_zero_on_akshare_failure(monkeypatch):
    """AKShare 全部失败时，update_latest_data 不崩溃，返回失败状态。"""
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        db = ETFDatabase(db_path)

        # 预先写入一条历史数据，使 fetch_latest 尝试增量更新
        seed = pd.DataFrame({
            "ticker": ["512480.SH"],
            "date": ["2026-06-25"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100],
            "adj_close": [1.0],
            "source": ["TEST"],
            "adjust_type": ["qfq"],
        })
        db.save_market_data(seed)

        # 模拟 AKShare 全部失败：health_check 通过但 fetch_all_data 返回空表
        def mock_health_check(self, test_code='512480'):
            return {"ok": True, "elapsed": 0.1, "message": "mock health ok"}

        def mock_fetch_all_data(self, start_date, end_date=None, db=None, delay=0.5):
            self.success_tickers = []
            self.failed_tickers = ["512480"]
            return pd.DataFrame()

        monkeypatch.setattr(DataFetcher, "health_check", mock_health_check)
        monkeypatch.setattr(DataFetcher, "fetch_all_data", mock_fetch_all_data)

        result = update_latest_data(db=db)

        assert result["saved_count"] == 0
        assert result["status"] == "no_data"
        assert "512480" in result["failed_tickers"]


def test_import_local_ifind_csvs(monkeypatch):
    """自动扫描本地 ifind_*.csv 并正确导入数据库。"""
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        db = ETFDatabase(db_path)

        # 构造符合 iFinD 格式的 CSV
        csv_path = os.path.join(d, "ifind_batch_test.csv")
        df = pd.DataFrame({
            "open": [2.1, 2.2],
            "high": [2.2, 2.3],
            "low": [2.0, 2.1],
            "close": [2.15, 2.25],
            "volume": [1000, 2000],
            "thscode": ["512480.SH", "512480.SH"],
            "time": ["2026-06-28", "2026-06-29"],
            "thsname_cn": ["NA", "NA"],
        })
        df.to_csv(csv_path, index=False)

        result = import_local_ifind_csvs(db=db, csv_dir=d, pattern="ifind_*.csv")

        assert result["status"] == "success"
        assert result["total_count"] == 2
        assert len(result["files"]) == 1
        assert result["files"][0]["count"] == 2

        latest = db.get_latest_date()
        assert latest == "2026-06-29"


def test_import_local_ifind_csvs_no_files():
    """目录下没有匹配文件时返回 no_files 状态。"""
    with tempfile.TemporaryDirectory() as d:
        result = import_local_ifind_csvs(db=None, csv_dir=d, pattern="ifind_*.csv")
        assert result["status"] == "no_files"
        assert result["total_count"] == 0


def test_partial_success_saves_data(monkeypatch):
    """部分 ticker 成功、部分失败时，update_latest_data 保存成功数据并记录失败。"""
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        db = ETFDatabase(db_path)

        seed = pd.DataFrame({
            "ticker": ["512480.SH"],
            "date": ["2026-06-25"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100],
            "adj_close": [1.0],
            "source": ["TEST"],
            "adjust_type": ["qfq"],
        })
        db.save_market_data(seed)

        # 模拟 fetch_all_data：部分成功，部分失败
        def mock_health_check(self, test_code='512480'):
            return {"ok": True, "elapsed": 0.1, "message": "mock health ok"}

        def mock_fetch_all_data(self, start_date, end_date=None, db=None, delay=0.5):
            self.success_tickers = ["512480"]
            self.failed_tickers = ["515230"]
            return pd.DataFrame({
                "ticker": ["512480.SH"],
                "date": [pd.to_datetime("2026-06-28")],
                "open": [1.0],
                "high": [1.1],
                "low": [0.9],
                "close": [1.05],
                "volume": [100],
                "adj_close": [1.05],
                "source": ["AKShare"],
                "adjust_type": ["qfq"],
            })

        monkeypatch.setattr(DataFetcher, "health_check", mock_health_check)
        monkeypatch.setattr(DataFetcher, "fetch_all_data", mock_fetch_all_data)

        result = update_latest_data(db=db)

        assert result["saved_count"] == 1
        assert result["status"] == "partial"
        assert "512480" in result["success_tickers"]
        assert "515230" in result["failed_tickers"]

        latest = db.get_latest_date()
        assert latest == "2026-06-28"

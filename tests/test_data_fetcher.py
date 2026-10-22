#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_data_fetcher.py — data fetcher fallback & iFinD CSV import tests."""

import os
import json
import subprocess as _subprocess_mod
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import data_fetcher
import ifind_gateway
from data_fetcher import (DataFetcher, import_local_ifind_csvs, update_latest_data,
                          update_latest_data_v2, detect_splice_breaks)
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


# ==================== 行情更新修复 v2 测试（设计文档 3.4 节） ====================
#
# 所有场景均使用 tmp 临时 SQLite 库与显式构造的 mock 数据；
# 不发起任何真实网络/子进程调用（monkeypatch 网关与 subprocess）。

# 2026-06-24 ~ 2026-07-10 的交易日（工作日序列，含 512480 拆分窗口 7/2→7/3）
TRADING_DAYS = pd.bdate_range("2026-06-24", "2026-07-10").strftime("%Y-%m-%d").tolist()

BENCH = "000300.SH"


def _make_fwd_rows(ticker, dates, closes, volume=100000):
    """构造 iFinD forward 标准化 DataFrame 行。"""
    rows = []
    for d, c in zip(dates, closes):
        rows.append({
            "ticker": ticker,
            "date": d,
            "open": c - 0.005,
            "high": c + 0.005,
            "low": c - 0.005,
            "close": c,
            "volume": volume,
        })
    return rows


def _seed_ticker(db, ticker, dates, closes, source="TEST", adjust_type="qfq", volume=100):
    """向临时库写入已有历史行情（显式构造，非生成/插值）。"""
    df = pd.DataFrame({
        "ticker": ticker,
        "date": dates,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": volume,
        "adj_close": closes,
        "source": source,
        "adjust_type": adjust_type,
    })
    db.save_market_data(df)


class _FakeIFindGateway:
    """mock 网关：只返回预置 DataFrame，绝不触网/起子进程。

    真实价格架构 v3：支持 none/fwd 双序列——none_df 预置 adjust='none'
    返回值；缺省时 none 与 forward 共用 df。
    """

    def __init__(self, df, failed=None, available=True, none_df=None,
                 credentials_configured=True):
        self._df = df
        self._none_df = none_df
        self._failed = failed or {}
        self._available = available
        self._credentials_configured = credentials_configured
        self.calls = []

    def is_available(self):
        return {"available": self._available, "message": "mock gateway"}

    def check_credentials(self, force=False):
        if self._credentials_configured:
            return {"configured": True, "source": "env",
                    "message": "mock credentials ok"}
        return {"configured": False, "source": "none",
                "message": "mock 缺少 Kimi 数据服务凭证（测试桩）"}

    def get_price(self, tickers, start_date, end_date, adjust="forward", **kwargs):
        self.calls.append({"tickers": list(tickers), "start": start_date,
                           "end": end_date, "adjust": adjust})
        src = self._none_df if (adjust == "none" and self._none_df is not None) else self._df
        df = src[src["ticker"].isin(tickers)] if not src.empty else src
        failed = {t: self._failed[t] for t in tickers if t in self._failed}
        succeeded = [t for t in tickers if t not in failed]
        return {"df": df, "succeeded": succeeded, "failed": failed,
                "elapsed_seconds": 0.1}


def _bench_fwd_df():
    """基准 000300.SH 的 forward mock：收盘价 4.00 起每日 +0.01。"""
    closes = [4.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
    return pd.DataFrame(_make_fwd_rows(BENCH, TRADING_DAYS, closes))


def _patch_gateway(monkeypatch, df, failed=None, available=True, none_df=None,
                   credentials_configured=True):
    fake = _FakeIFindGateway(df, failed=failed, available=available, none_df=none_df,
                             credentials_configured=credentials_configured)
    monkeypatch.setattr(data_fetcher, "ifind_gateway", fake)
    return fake


class TestIFindGateway:
    """场景 1/2/7：网关层的超时重试、整体耗时预算、单组失败隔离。"""

    @staticmethod
    def _plugin_csv_rows(tickers_csv):
        """模拟插件写出的 CSV（iFinD 原始列名 thscode/time）。"""
        rows = []
        for t in tickers_csv.split(","):
            rows.append({
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
                "volume": 50000, "thscode": t, "time": "2026-06-05",
                "thsname_cn": "NA",
            })
        return rows

    def test_timeout_retry_then_success(self, monkeypatch):
        """单组超时重试：前 2 次 TimeoutExpired，第 3 次成功，最终标的成功。"""
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            if len(calls) <= 2:
                raise _subprocess_mod.TimeoutExpired(cmd, timeout)
            params = json.loads(cmd[cmd.index("--params-json") + 1])
            pd.DataFrame(self._plugin_csv_rows(params["ticker"])).to_csv(
                params["file_path"], index=False, encoding="utf-8-sig")
            return _subprocess_mod.CompletedProcess(cmd, 0)

        monkeypatch.setattr(ifind_gateway, "_run_command", fake_run)
        monkeypatch.setattr(ifind_gateway.time, "sleep", lambda s: None)  # 跳过退避等待
        monkeypatch.setattr(ifind_gateway, "check_credentials",
                            lambda force=False: {"configured": True, "source": "env",
                                                 "message": ""})

        res = ifind_gateway.get_price(["512480.SH"], "2026-06-01", "2026-06-05",
                                      timeout=10, max_retries=2)
        assert len(calls) == 3  # 1 次原始 + 2 次重试
        assert res["succeeded"] == ["512480.SH"]
        assert res["failed"] == {}
        assert list(res["df"].columns) == ["ticker", "date", "open", "high", "low", "close", "volume"]
        assert res["df"].iloc[0]["date"] == "2026-06-05"  # 规范为 YYYY-MM-DD

    def test_total_budget_marks_remaining_groups_failed(self, monkeypatch):
        """整体耗时预算用尽：剩余组不再执行子进程，直接标记失败。"""
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            return _subprocess_mod.CompletedProcess(cmd, 0)

        monkeypatch.setattr(ifind_gateway, "_run_command", fake_run)
        monkeypatch.setattr(ifind_gateway, "check_credentials",
                            lambda force=False: {"configured": True, "source": "env",
                                                 "message": ""})

        res = ifind_gateway.get_price(["AAA.SH", "BBB.SH", "CCC.SH", "DDD.SH"],
                                      "2026-06-01", "2026-06-05",
                                      total_budget_seconds=-1)
        assert calls == []  # 预算已耗尽，未发起任何子进程
        assert len(res["failed"]) == 4
        assert all("耗时预算" in msg for msg in res["failed"].values())

    def test_single_group_failure_does_not_block_others(self, monkeypatch):
        """4 只标的分 2 组：第 1 组全部失败，第 2 组不受影响。"""
        def fake_run(cmd, timeout):
            params = json.loads(cmd[cmd.index("--params-json") + 1])
            if "AAA.SH" in params["ticker"]:
                raise RuntimeError("模拟插件崩溃")
            pd.DataFrame(self._plugin_csv_rows(params["ticker"])).to_csv(
                params["file_path"], index=False, encoding="utf-8-sig")
            return _subprocess_mod.CompletedProcess(cmd, 0)

        monkeypatch.setattr(ifind_gateway, "_run_command", fake_run)
        monkeypatch.setattr(ifind_gateway.time, "sleep", lambda s: None)
        monkeypatch.setattr(ifind_gateway, "check_credentials",
                            lambda force=False: {"configured": True, "source": "env",
                                                 "message": ""})

        res = ifind_gateway.get_price(["AAA.SH", "BBB.SH", "CCC.SH", "DDD.SH"],
                                      "2026-06-01", "2026-06-05", max_retries=0)
        assert sorted(res["failed"].keys()) == ["AAA.SH", "BBB.SH", "CCC.SH"]
        assert res["succeeded"] == ["DDD.SH"]
        assert res["df"]["ticker"].unique().tolist() == ["DDD.SH"]

    def test_missing_credentials_fails_fast_without_subprocess(self, monkeypatch):
        """缺凭证：全部标的直接标记失败，零子进程调用、零重试，消息含修复提示。"""
        calls = []
        monkeypatch.setattr(ifind_gateway, "_run_command",
                            lambda cmd, timeout: calls.append(cmd))
        monkeypatch.setattr(ifind_gateway, "check_credentials", lambda force=False: {
            "configured": False, "source": "none",
            "message": "缺少 Kimi 数据服务凭证；请写入 ~/.kimi/agent-gw.json（测试消息）"})

        res = ifind_gateway.get_price(["AAA.SH", "BBB.SH"], "2026-06-01",
                                      "2026-06-05", max_retries=3)
        assert calls == []  # 零子进程、零重试
        assert sorted(res["failed"].keys()) == ["AAA.SH", "BBB.SH"]
        assert res["succeeded"] == []
        assert res["df"].empty
        assert "凭证" in res["message"]
        assert "凭证" in res["failed"]["AAA.SH"]

    def test_check_credentials_prefers_env_and_never_echoes_key(self, monkeypatch, tmp_path):
        """凭证检测分层：env > 配置文件；返回值不回显密钥。"""
        cred_file = tmp_path / "agent-gw.json"
        monkeypatch.setattr(ifind_gateway, "_CRED_FILE", str(cred_file))
        monkeypatch.delenv(ifind_gateway._CRED_ENV_VAR, raising=False)
        monkeypatch.setattr(ifind_gateway, "_credentials_cache", None)

        # 无 env 无文件 → none
        res = ifind_gateway.check_credentials(force=True)
        assert res["configured"] is False and res["source"] == "none"
        assert "KIMI_API_KEY" in res["message"] or "agent-gw.json" in res["message"]

        # 仅文件 → file，消息不回显密钥
        cred_file.write_text(json.dumps({"api_key": "sk-TESTSECRET-0123456789",
                                         "base_url": "https://example.com/coding"}),
                             encoding="utf-8")
        res = ifind_gateway.check_credentials(force=True)
        assert res["configured"] is True and res["source"] == "file"
        assert "sk-TESTSECRET" not in json.dumps(res, ensure_ascii=False)

        # env 优先于文件
        monkeypatch.setenv(ifind_gateway._CRED_ENV_VAR, "sk-ENVSECRET-0123456789")
        res = ifind_gateway.check_credentials(force=True)
        assert res["configured"] is True and res["source"] == "env"
        assert "sk-ENVSECRET" not in json.dumps(res, ensure_ascii=False)

        # 文件损坏/无 api_key → file_invalid
        monkeypatch.delenv(ifind_gateway._CRED_ENV_VAR, raising=False)
        cred_file.write_text('{"base_url": "https://example.com/coding"}', encoding="utf-8")
        res = ifind_gateway.check_credentials(force=True)
        assert res["configured"] is False and res["source"] == "file_invalid"

    def test_is_available_reports_program_and_credentials_separately(self, monkeypatch, tmp_path):
        """is_available 分开展示：程序安装状态与凭证配置状态。"""
        plugin_dir = tmp_path / "plugin"
        scripts_dir = plugin_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        tool_script = scripts_dir / "ifind_tool.py"
        tool_script.write_text("# mock", encoding="utf-8")
        python = tmp_path / "python.exe"
        python.write_text("# mock", encoding="utf-8")
        monkeypatch.setattr(ifind_gateway, "get_plugin_dir", lambda: str(plugin_dir))
        monkeypatch.setattr(ifind_gateway, "get_python_path", lambda: str(python))
        monkeypatch.setattr(ifind_gateway, "get_tool_script", lambda: str(tool_script))

        cred_file = tmp_path / "agent-gw.json"
        monkeypatch.setattr(ifind_gateway, "_CRED_FILE", str(cred_file))
        monkeypatch.delenv(ifind_gateway._CRED_ENV_VAR, raising=False)
        monkeypatch.setattr(ifind_gateway, "_credentials_cache", None)

        res = ifind_gateway.is_available(force=True)
        assert res["program_installed"] is True
        assert res["credentials_configured"] is False
        assert res["credentials"]["source"] == "none"

        cred_file.write_text(json.dumps({"api_key": "sk-X", "base_url": "https://b/c"}),
                             encoding="utf-8")
        res = ifind_gateway.is_available(force=True)
        assert res["credentials_configured"] is True
        assert res["credentials"]["source"] == "file"


class TestUpdateLatestDataV2:
    """场景 3/4/5/6：缺口检测补齐、拆分拼接无假跳变、幂等、逐只明细。"""

    def test_gap_detection_and_middle_gap_fill(self, monkeypatch, tmp_path):
        """已有 6/29 与 7/7（7/7 为已有断裂点后数据），补 6/30~7/6 与 7/8~7/10。

        两个缺失段各自锚定（6/29 与 7/7），splice_factors 应披露分段因子。
        """
        db = ETFDatabase(str(tmp_path / "test.db"))

        seed_days = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-07-07"]
        seed_closes = [2.00, 2.01, 2.02, 2.03, 2.10]
        _seed_ticker(db, "512480.SH", seed_days, seed_closes)
        _seed_ticker(db, BENCH, seed_days, [4.00, 4.01, 4.02, 4.03, 4.10])

        closes = [2.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", TRADING_DAYS, closes) + _bench_fwd_df().to_dict("records"))
        # 真实价格架构 v3：双序列拉取。本场景无拆分，none 真实价与 fwd 数值相同
        # （none/fwd 比率恒为 1.0，不触发拆分登记）。
        _patch_gateway(monkeypatch, fwd_df, none_df=fwd_df.copy())

        result = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")

        assert result["status"] == "success"
        # 缺失 = 6/30,7/1,7/2,7/3,7/6,7/8,7/9,7/10 共 8 天（7/7 已有）
        expected_gaps = ["2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03",
                         "2026-07-06", "2026-07-08", "2026-07-09", "2026-07-10"]
        info = result["per_ticker"]["512480.SH"]
        assert info["new_rows"] == 8
        assert info["latest_date"] == "2026-07-10"
        assert info["gaps"] == expected_gaps
        assert info["error"] is None

        # 分段因子披露：两个锚定日各一个因子
        factors = result["splice_factors"]["512480.SH"]
        assert isinstance(factors, dict)
        assert set(factors.keys()) == {"2026-06-29", "2026-07-07"}
        assert factors["2026-06-29"] == pytest.approx(2.03 / 2.03, rel=1e-9)
        assert factors["2026-07-07"] == pytest.approx(2.10 / 2.09, rel=1e-9)

        # 全库缺口清单（无论是否补入都要报告）
        gap_keys = {(g["ticker"], g["date"]) for g in result["gaps"]}
        assert ("512480.SH", "2026-06-30") in gap_keys
        assert len(result["gaps"]) == 16  # 两只标的各 8 天

        # 拼接数值正确性（真实价格架构 v3）：close/open/high/low/volume 存 iFinD
        # none 真实值；adj_close = fwd × 校准因子（此处首段因子=1.0）。
        row = db.get_market_data(ticker="512480.SH", start_date="2026-06-30", end_date="2026-06-30")
        assert len(row) == 1
        assert row.iloc[0]["close"] == pytest.approx(2.04, rel=1e-9)
        assert row.iloc[0]["adj_close"] == pytest.approx(2.04, rel=1e-9)  # fwd(6/30)×1.0
        assert row.iloc[0]["source"] == "iFinD-KimiGateway"
        assert row.iloc[0]["adjust_type"] == "real_spliced"
        assert row.iloc[0]["volume"] == pytest.approx(100000.0)  # 真实成交量，绝不乘价格因子

        # 第二段（锚定 7/7，因子=2.10/2.09）：真实价照存，adj_close 承担口径衔接
        row2 = db.get_market_data(ticker="512480.SH", start_date="2026-07-08", end_date="2026-07-08")
        assert len(row2) == 1
        assert row2.iloc[0]["close"] == pytest.approx(2.10, rel=1e-9)  # 真实价 = fwd 值（本场景 none==fwd）
        assert row2.iloc[0]["adj_close"] == pytest.approx(2.10 * 2.10 / 2.09, rel=1e-9)
        assert row2.iloc[0]["volume"] == pytest.approx(100000.0)  # 真实成交量，不随因子缩放

        # 评分口径衔接：adj_close(7/7 已存 2.10) → adj_close(7/8) ≈ 2.110，无跳变
        assert abs(row2.iloc[0]["adj_close"] / 2.10 - 1) < 0.01

        # 已有历史行不被覆盖（INSERT OR IGNORE 只增不改）
        old = db.get_market_data(ticker="512480.SH", start_date="2026-06-29", end_date="2026-06-29")
        assert old.iloc[0]["close"] == 2.03
        assert old.iloc[0]["source"] == "TEST"

    def test_split_splice_no_false_jump(self, monkeypatch, tmp_path):
        """拆分拼接（真实价格架构 v3）：mock 缺口期含 1:2 份额拆分。

        构造：数据库 512480 停在 6/29，close=adj_close=2.00（拆前真实口径）；
        iFinD none 序列在 7/2→7/3 发生 1:2 拆分（真实价 2.00 → 1.005）；
        forward 序列全历史统一复权到拆后口径（拆前 1.00，拆后 1.005）。

        新语义：
        - close/open/high/low/volume 存 none 真实值（7/3 close=1.005，含真实拆分跳变）；
        - adj_close = fwd × 校准因子（锚 6/29：库内 adj 2.00 / fwd 1.00 = 2.0），
          评分口径 6/29(2.00) → 7/3(2.01) 连续，无 -49.75% 假跳变；
        - 拆分事件自动登记 corporate_actions（ratio=2.0），且 detect_splice_breaks
          不再把已登记的拆分日误报为口径断裂。
        """
        db = ETFDatabase(str(tmp_path / "test.db"))

        seed_days = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29"]
        _seed_ticker(db, "512480.SH", seed_days, [2.00, 2.00, 2.00, 2.00])
        _seed_ticker(db, BENCH, seed_days, [4.00, 4.01, 4.02, 4.03])

        pre_days = [d for d in TRADING_DAYS if d <= "2026-07-02"]
        post_days = [d for d in TRADING_DAYS if d >= "2026-07-03"]
        # none 真实序列：拆前 2.00，拆后 1.005（真实拆分跳变 -49.75%）；含基准行
        none_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", pre_days, [2.00] * len(pre_days))
            + _make_fwd_rows("512480.SH", post_days, [1.005] * len(post_days))
            + _bench_fwd_df().to_dict("records"))
        # forward 序列：全历史统一复权到拆后口径（拆前 1.00，拆后 1.005）
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", pre_days, [1.00] * len(pre_days))
            + _make_fwd_rows("512480.SH", post_days, [1.005] * len(post_days))
            + _bench_fwd_df().to_dict("records"))
        _patch_gateway(monkeypatch, fwd_df, none_df=none_df)

        result = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")
        assert result["status"] == "success"

        df = db.get_market_data(ticker="512480.SH")
        df = df.sort_values("date").reset_index(drop=True)
        assert df["date"].max().strftime("%Y-%m-%d") == "2026-07-10"

        # 关键断言 1：存储为真实价格——拆分日 close=1.005（真实拆后价），
        # 真实序列在 7/2→7/3 含 -49.75% 真实拆分跳变（不再要求 close 连续）
        split_day = df[df["date"] == "2026-07-03"].iloc[0]
        assert split_day["close"] == pytest.approx(1.005, rel=1e-9)
        assert split_day["adjust_type"] == "real_spliced"
        assert split_day["volume"] == pytest.approx(100000.0)  # 真实成交量，绝不乘 2.0 因子

        pre_day = df[df["date"] == "2026-06-30"].iloc[0]
        assert pre_day["close"] == pytest.approx(2.00, rel=1e-9)  # 拆前真实价

        # 关键断言 2：adj_close（评分口径）跨拆分连续——
        # 校准因子 = 库内 adj(6/29)=2.00 / fwd(6/29)=1.00 = 2.0，
        # adj(6/29)=2.00 → adj(7/3)=1.005×2.0=2.01，无假跳变
        seed_adj = 2.00  # 6/29 库内 adj_close
        assert split_day["adj_close"] == pytest.approx(2.01, rel=1e-9)
        assert abs(split_day["adj_close"] / seed_adj - 1) < 0.05
        # 拆前日期 adj_close = fwd×2.0，同样衔接
        assert pre_day["adj_close"] == pytest.approx(2.00, rel=1e-9)

        # 锚定因子 = 2.00 / 1.00 = 2.0，且为单段单因子（float 披露）
        assert result["splice_factors"]["512480.SH"] == pytest.approx(2.0, rel=1e-9)

        # 关键断言 3：拆分事件已登记 corporate_actions（effective_date=7/3, ratio=2.0）
        actions = db.get_corporate_actions(ticker="512480.SH")
        assert len(actions) == 1
        a = actions.iloc[0]
        assert str(a["effective_date"])[:10] == "2026-07-03"
        assert a["action_type"] == "split"
        assert a["ratio"] == pytest.approx(2.0, rel=1e-9)

        # 关键断言 4：detect_splice_breaks 排除已登记拆分日——
        # 7/3 的 -49.75% 是真实拆分，不得再被误报为口径断裂
        assert detect_splice_breaks(db, ["512480.SH"]) == {}

    def test_idempotent_reimport(self, monkeypatch, tmp_path):
        """重复导入幂等：同一 mock 数据跑两遍，第二遍 saved_count=0、状态 no_data。"""
        db = ETFDatabase(str(tmp_path / "test.db"))

        seed_days = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29"]
        _seed_ticker(db, "512480.SH", seed_days, [2.00, 2.01, 2.02, 2.03])
        _seed_ticker(db, BENCH, seed_days, [4.00, 4.01, 4.02, 4.03])

        closes = [2.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", TRADING_DAYS, closes) + _bench_fwd_df().to_dict("records"))
        _patch_gateway(monkeypatch, fwd_df)

        first = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")
        assert first["saved_count"] == 18  # 每只 9 天（6/30~7/10）

        second = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")
        assert second["saved_count"] == 0
        assert second["status"] == "no_data"
        assert "已是最新" in second["message"]
        for info in second["per_ticker"].values():
            assert info["new_rows"] == 0
            assert info["gaps"] == []
            assert info["error"] is None

        # 数据库总行数不变（INSERT OR IGNORE 不产生重复行）
        df_all = db.get_market_data(ticker=["512480.SH", BENCH])
        assert len(df_all) == 2 * len(TRADING_DAYS)

    def test_single_ticker_failure_and_per_ticker_report(self, monkeypatch, tmp_path):
        """单只标网关失败不阻断其他标的；逐只 new_rows/latest_date/错误正确。"""
        db = ETFDatabase(str(tmp_path / "test.db"))

        seed_days = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29"]
        _seed_ticker(db, "512480.SH", seed_days, [2.00, 2.01, 2.02, 2.03])
        _seed_ticker(db, "515880.SH", TRADING_DAYS, [0.5 + 0.01 * i for i in range(len(TRADING_DAYS))])
        _seed_ticker(db, BENCH, seed_days, [4.00, 4.01, 4.02, 4.03])

        closes = [2.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", TRADING_DAYS, closes) + _bench_fwd_df().to_dict("records"))
        _patch_gateway(monkeypatch, fwd_df, failed={"515880.SH": "模拟网关失败"})

        progress = []
        result = update_latest_data_v2(
            db=db, tickers=["512480.SH", "515880.SH", BENCH], source="ifind",
            on_progress=lambda t, done, total, msg: progress.append((t, done, total)))

        # 单只失败 → partial，其余照常写入
        assert result["status"] == "partial"
        assert result["saved_count"] == 18
        assert "模拟网关失败" in (result["per_ticker"]["515880.SH"]["error"] or "")

        ok = result["per_ticker"]["512480.SH"]
        assert ok["new_rows"] == 9
        assert ok["latest_date"] == "2026-07-10"
        assert ok["error"] is None

        # 515880 已有全区间数据，网关失败后 latest_date 保持为空（未参与拼接）
        assert result["per_ticker"]["515880.SH"]["new_rows"] == 0

        # 进度回调按标的逐个触发
        assert [p[1] for p in progress] == [1, 2, 3]

        # 515880 数据库数据未被改动（网关失败的标的不写库）
        row = db.get_market_data(ticker="515880.SH", start_date="2026-07-10", end_date="2026-07-10")
        assert row.iloc[0]["close"] == pytest.approx(0.5 + 0.01 * 12, rel=1e-9)
        assert row.iloc[0]["source"] == "TEST"


class TestDetectSpliceBreaks:
    """库内口径断裂预检（Trae 复审要求：旧数据已有断裂时能够发现并阻止误报成功）。"""

    def test_detects_intra_db_splice_break(self, tmp_path):
        """构造 515880 式断裂（拆分前口径后直接接拆分后口径，-49%），必须被发现。"""
        db = ETFDatabase(str(tmp_path / "test.db"))
        days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]
        closes = [1.60, 1.58, 1.59, 1.50, 0.76]  # 7/6→7/7 = -49.3% 口径断裂
        _seed_ticker(db, "515880.SH", days, closes)

        breaks = detect_splice_breaks(db, ["515880.SH"])
        assert "515880.SH" in breaks
        b = breaks["515880.SH"][0]
        assert b["date"] == "2026-07-07"
        assert b["prev_date"] == "2026-07-06"
        assert b["return"] == pytest.approx(-0.4933, abs=1e-3)

    def test_normal_data_no_false_positive(self, tmp_path):
        """正常行情（涨跌幅均在 10% 以内）不得误报断裂。"""
        db = ETFDatabase(str(tmp_path / "test.db"))
        days = [f"2026-06-{d:02d}" for d in range(22, 30) if d not in (27, 28)]
        closes = [2.00, 2.05, 1.98, 2.10, 2.02, 2.08]  # 最大单日 +6%
        _seed_ticker(db, "512480.SH", days, closes)
        assert detect_splice_breaks(db, ["512480.SH"]) == {}

    def test_update_v2_blocked_from_claiming_success_with_break(self, monkeypatch, tmp_path):
        """库内存在断裂时，即使拼接全部成功，status 也不得为 success，
        message 必须提示先修复历史数据（阻止误报成功）。"""
        db = ETFDatabase(str(tmp_path / "test.db"))
        seed_days = ["2026-06-29", "2026-07-07"]
        _seed_ticker(db, "512480.SH", seed_days, [2.03, 2.10])
        _seed_ticker(db, BENCH, seed_days, [4.03, 4.10])
        # 人为制造库内断裂：7/7 close 改为 1.00（2.03→1.00 = -50.7%，拆分式口径断裂）
        with db._connect() as conn:
            conn.execute("UPDATE market_data SET close = 1.00 WHERE ticker='512480.SH' AND date='2026-07-07'")
            conn.commit()

        closes = [2.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", TRADING_DAYS, closes) + _bench_fwd_df().to_dict("records"))
        _patch_gateway(monkeypatch, fwd_df)

        result = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")
        assert result["status"] == "partial"  # 有断裂 → 不得 success
        assert "512480.SH" in result["precheck_breaks"]
        assert "断裂" in result["message"]
        assert result["saved_count"] > 0  # 拼接本身仍执行，只是不虚报成功

    def test_update_v2_success_when_no_break(self, monkeypatch, tmp_path):
        """修复后的干净库（无断裂）可以正常返回 success。"""
        db = ETFDatabase(str(tmp_path / "test.db"))
        seed_days = ["2026-06-29"]
        _seed_ticker(db, "512480.SH", seed_days, [2.03])
        _seed_ticker(db, BENCH, seed_days, [4.03])

        closes = [2.00 + 0.01 * i for i in range(len(TRADING_DAYS))]
        fwd_df = pd.DataFrame(
            _make_fwd_rows("512480.SH", TRADING_DAYS, closes) + _bench_fwd_df().to_dict("records"))
        _patch_gateway(monkeypatch, fwd_df)

        result = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH], source="ifind")
        assert result["status"] == "success"
        assert result["precheck_breaks"] == {}

    def test_update_v2_credentials_missing_fails_fast(self, monkeypatch, tmp_path):
        """缺凭证：直接返回 credentials_missing，不发起任何网关请求、不写库。"""
        db = ETFDatabase(str(tmp_path / "test.db"))
        _seed_ticker(db, "512480.SH", ["2026-06-29"], [2.03])
        _seed_ticker(db, BENCH, ["2026-06-29"], [4.03])

        fake = _patch_gateway(monkeypatch, pd.DataFrame(),
                              credentials_configured=False)

        result = update_latest_data_v2(db=db, tickers=["512480.SH", BENCH],
                                       source="ifind")
        assert result["status"] == "credentials_missing"
        assert "凭证" in result["message"]
        assert fake.calls == []  # 零网关请求、零批量重试
        assert db.get_latest_date() == "2026-06-29"  # 未写入任何新数据

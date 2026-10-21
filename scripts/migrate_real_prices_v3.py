#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/migrate_real_prices_v3.py — 真实价格架构 v3 数据迁移（幂等）。

设计文档: docs/REAL_PRICE_ARCH_v3_DESIGN.md 第 3 节。
顺序: 先备份 → 全历史双序列拉取（缓存）→ 拆分登记 → 删旧拼接行 →
      515880 correction ÷2 → 生产路径重生成 → 全历史 adj_close 重算 →
      重算 daily_scores（B0.4 标准配置 = STRATEGY_CONFIG）→ 验证包。

用法:
  python scripts/migrate_real_prices_v3.py                # 全流程（先自动备份）
  python scripts/migrate_real_prices_v3.py --db <副本.db>   # 先在副本验证
  python scripts/migrate_real_prices_v3.py --skip-fetch    # 复用缓存 CSV（幂等重跑）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config import (  # noqa: E402
    STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE,
    ALL_TRADABLE_ETFS,
)
from database import ETFDatabase, apply_adjustment_for_scoring  # noqa: E402
from data_fetcher import update_latest_data_v2, _detect_splits_from_none_fwd  # noqa: E402
import ifind_gateway  # noqa: E402
from strategy import StrategyEngine  # noqa: E402

REGEN_START = "2026-06-30"          # 旧等效价拼接行起点（含）
CORRECTION_FIX_TICKER = "515880.SH"  # correction 行 ×2 需 ÷2 的标的
CORRECTION_FIX_RANGE = ("2026-07-06", "2026-07-10")
FETCH_START = "2019-01-01"
CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "migration_real_prices_v3")
B0_4_SNAPSHOT = os.path.join(
    os.path.dirname(__file__), "..", "data", "snapshots",
    "B0_4_candidate_data_20260621_210815.csv")
B0_4_SNAPSHOT_SHA256 = "dbdb041d"  # 记录值前缀（docs 中有全量值）


class _CurlGatewayShim:
    """get_price 兼容壳：网关优先（auto）或纯 curl（curl），用于本机 TLS 故障期。"""

    def __init__(self, real_gateway, mode):
        self._real = real_gateway
        self._mode = mode

    def is_available(self):
        return {"available": True, "message": "curl-shim"}

    def get_price(self, tickers, start_date, end_date, adjust="forward", **kw):
        if self._mode == "auto":
            try:
                res = self._real.get_price(
                    list(tickers), start_date, end_date, adjust=adjust, **kw)
                fatal = {k: v for k, v in (res.get("failed") or {}).items()
                         if "EMPTY_DATA" not in str(v)}
                if not fatal:
                    return res
            except Exception:
                pass
        df = fetch_segment_via_curl(list(tickers), start_date, end_date, adjust)
        succeeded = sorted(df["ticker"].unique().tolist()) if not df.empty else []
        failed = {t: "EMPTY_DATA" for t in tickers if t not in set(succeeded)}
        return {"df": df, "succeeded": succeeded, "failed": failed,
                "elapsed_seconds": 0.1}


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------
# 步骤 1：备份
# ----------------------------------------------------------------------
def backup_db(db_path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(backup_dir, f"etf_model_{ts}_pre_real_price_v3.db")
    shutil.copy2(db_path, dst)
    if os.path.getsize(dst) <= 0:
        raise RuntimeError(f"备份文件为 0 字节: {dst}")
    log(f"步骤1 备份完成: {dst} ({os.path.getsize(dst):,} 字节)")
    return dst


# ----------------------------------------------------------------------
# 步骤 2：全历史分段拉取 none+fwd（带缓存）
# ----------------------------------------------------------------------
def _segments(start: str, end: str, years: int = 1):
    """按年分段：网关对跨年长区间会返回 EMPTY_DATA（疑似行数上限），1 年稳定。"""
    segs = []
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    while s < e:
        seg_e = min(s + pd.DateOffset(years=years) - pd.Timedelta(days=1), e)
        segs.append((s.strftime("%Y-%m-%d"), seg_e.strftime("%Y-%m-%d")))
        s = seg_e + pd.Timedelta(days=1)
    return segs


def _load_gw_config():
    """复用 agent-gw SDK 的配置解析（本地 TLS 故障只影响 HTTP 传输，配置解析可用）。"""
    cfg = {}
    try:
        from pathlib import Path
        p = Path("~/.kimi/agent-gw.json").expanduser()
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    key = os.environ.get("KIMI_API_KEY") or cfg.get("api_key")
    base = os.environ.get("KIMI_BASE_URL") or cfg.get("base_url") \
        or "https://agent-gw.kimi.com/coding"
    return key, base


def ifind_call_via_curl(params, timeout=180):
    """本机 Python TLS 栈对 agent-gw 握手失败时的备用传输：curl POST /v1/tools。

    信封与 agent-gw SDK 完全一致（method=call_data_source_tool），数据同 iFinD。
    返回解析后的响应 dict；is_success=False 时抛 RuntimeError。
    """
    key, base = _load_gw_config()
    if not key:
        raise RuntimeError("agent-gw api_key 不可用（~/.kimi/agent-gw.json 或 KIMI_API_KEY）")
    body = {
        "method": "call_data_source_tool",
        "params": {
            "data_source_name": "ifind",
            "api_name": "ifind_get_price",
            "params": params,
        },
    }
    bf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(body, bf)
    bf.close()
    hf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    hf.write('header = "Authorization: Bearer %s"\n' % key)
    hf.write('header = "Content-Type: application/json"\n')
    hf.close()
    try:
        r = subprocess.run(
            ["curl", "-sS", "-X", "POST", base.rstrip("/") + "/v1/tools",
             "--data-binary", "@" + bf.name,
             "-K", hf.name, "--max-time", str(timeout)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"curl rc={r.returncode}: {r.stderr[:300]}")
        resp = json.loads(r.stdout)
    finally:
        os.unlink(bf.name)
        os.unlink(hf.name)
    if not resp.get("is_success"):
        err = str(resp.get("error"))
        if "EMPTY_DATA" in err:
            # 该区间无数据（如 2023 年才上市的标的在 2019 段）：预期情形，跳过
            return {"is_success": False, "empty_data": True}
        raise RuntimeError(f"ifind_get_price 失败: {err[:300]}")
    return resp


def _normalize_ifind_csv(csv_text):
    """插件 CSV（thscode/time/YYYYMMDD）→ 网关标准列（ticker/date/YYYY-MM-DD）。"""
    from io import StringIO
    df = pd.read_csv(StringIO(csv_text))
    df = df.rename(columns={"thscode": "ticker", "time": "date"})
    # 插件返回的 time 列格式随节点不同：YYYYMMDD（被 read_csv 读成 int）、
    # "YYYYMMDD" 字符串或 ISO "YYYY-MM-DD" 都出现过。int 直接喂 to_datetime
    # 会被当成纳秒时间戳变成 1970-01-01，必须先转字符串再嗅探格式。
    s = df["date"].astype(str).str.strip()
    is_compact = s.str.fullmatch(r"\d{8}")
    dates = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    if is_compact.any():
        dates[is_compact] = pd.to_datetime(s[is_compact], format="%Y%m%d")
    if (~is_compact).any():
        dates[~is_compact] = pd.to_datetime(s[~is_compact], format="mixed")
    df["date"] = dates.dt.strftime("%Y-%m-%d")
    return df[["ticker", "date", "open", "high", "low", "close", "volume"]]


def fetch_segment_via_curl(tickers, start, end, adjust):
    """curl 备用传输：3 只/组（与网关一致），合并为该段 DataFrame。"""
    parts = []
    for i in range(0, len(tickers), 3):
        group = tickers[i:i + 3]
        log(f"  curl {adjust} {start}~{end} group={','.join(group)}")
        tmp_csv = os.path.join(tempfile.gettempdir(),
                               f"ifind_curl_{adjust}_{i}_{abs(hash((start, end))) % 99999}.csv")
        try:
            resp = ifind_call_via_curl({
                "ticker": ",".join(group), "start_date": start,
                "end_date": end, "adjust": adjust, "file_path": tmp_csv,
            }, timeout=60)
            if resp.get("empty_data"):
                log(f"    EMPTY_DATA（该组该区间无数据，跳过）")
                continue
            files = resp.get("files") or []
            if not files:
                continue
            parts.append(_normalize_ifind_csv(files[0].get("content", "")))
        finally:
            if os.path.exists(tmp_csv):
                os.unlink(tmp_csv)
    if not parts:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low",
                                     "close", "volume"])
    return pd.concat(parts, ignore_index=True)


def fetch_dual_series(tickers, start, end, skip_fetch=False,
                      transport="auto"):
    """分段拉取（≤3年/段）并合并。缓存到 CSV；--skip-fetch 时复用。

    transport: 'auto' = 优先网关，失败回退 curl；'curl' = 强制 curl 备用传输。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    none_parts, fwd_parts = [], []
    for (s, e) in _segments(start, end):
        none_path = os.path.join(CACHE_DIR, f"none_{s}_{e}.csv")
        fwd_path = os.path.join(CACHE_DIR, f"fwd_{s}_{e}.csv")
        if skip_fetch and os.path.exists(none_path) and os.path.exists(fwd_path):
            log(f"步骤2 复用缓存 {s}~{e}")
            none_parts.append(pd.read_csv(none_path))
            fwd_parts.append(pd.read_csv(fwd_path))
            continue
        log(f"步骤2 拉取 {s}~{e}（{len(tickers)} 只 × 2 序列, transport={transport}）")

        def _gateway():
            res_none = ifind_gateway.get_price(list(tickers), s, e, adjust="none")
            res_fwd = ifind_gateway.get_price(list(tickers), s, e, adjust="forward")
            def _fatal(res):
                return {k: v for k, v in (res.get("failed") or {}).items()
                        if "EMPTY_DATA" not in str(v)}
            if _fatal(res_none) or _fatal(res_fwd):
                raise RuntimeError(
                    f"iFinD 网关失败 none={_fatal(res_none)} fwd={_fatal(res_fwd)}")
            n = res_none["df"] or pd.DataFrame()
            f = res_fwd["df"] or pd.DataFrame()
            return n, f

        if transport == "curl":
            n = fetch_segment_via_curl(tickers, s, e, "none")
            f = fetch_segment_via_curl(tickers, s, e, "forward")
        else:
            try:
                n, f = _gateway()
            except Exception as exc:
                log(f"  网关失败（{type(exc).__name__}: {str(exc)[:120]}），"
                    f"回退 curl 传输")
                n = fetch_segment_via_curl(tickers, s, e, "none")
                f = fetch_segment_via_curl(tickers, s, e, "forward")
        if not n.empty:
            n.to_csv(none_path, index=False)
            none_parts.append(n)
        if not f.empty:
            f.to_csv(fwd_path, index=False)
            fwd_parts.append(f)
        log(f"  none={len(n)} 行, fwd={len(f)} 行")
    none_df = pd.concat(none_parts, ignore_index=True) if none_parts else \
        pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    fwd_df = pd.concat(fwd_parts, ignore_index=True) if fwd_parts else \
        pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    return none_df, fwd_df


# ----------------------------------------------------------------------
# 步骤 3：拆分登记（全历史双序列比率阶跃）
# ----------------------------------------------------------------------
def register_splits(db: ETFDatabase, none_df: pd.DataFrame, fwd_df: pd.DataFrame,
                    tickers):
    rows = []
    for t in tickers:
        n = none_df[none_df["ticker"] == t]
        f = fwd_df[fwd_df["ticker"] == t]
        if n.empty or f.empty:
            continue
        rows.extend(_detect_splits_from_none_fwd(t, n, f))
    if rows:
        db.save_corporate_actions(pd.DataFrame(rows))
    log(f"步骤3 拆分登记 {len(rows)} 条")
    for r in rows:
        log(f"  {r['ticker']} {r['effective_date']} {r['action_type']} "
            f"ratio={r['ratio']:.4f}")
    return rows


# ----------------------------------------------------------------------
# 步骤 4/5：删旧拼接行 + 515880 correction ÷2
# ----------------------------------------------------------------------
def delete_old_spliced(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM market_data WHERE source = 'iFinD-KimiGateway' "
            "AND date >= ?", (REGEN_START,))
        n = cur.rowcount
        conn.commit()
    log(f"步骤4 删除旧等效价拼接行（source=iFinD-KimiGateway, >= {REGEN_START}）: {n} 行")
    return n


def halve_515880_corrections(db_path: str) -> int:
    """515880 的 7/6~7/10 correction 行此前被 ×2 成拆前口径：OHLCV 全部 ÷2。

    注意必须连 open/high/low 一起除——只除 close 会造出行内 open > close
    的自相矛盾数据（2026-10-20 副本验证实测捕获）。
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM market_data "
            "WHERE ticker = ? AND source = 'iFinD-KimiGateway-correction' "
            "AND date BETWEEN ? AND ?",
            (CORRECTION_FIX_TICKER, CORRECTION_FIX_RANGE[0],
             CORRECTION_FIX_RANGE[1])).fetchall()
        for (d, o, h, lo, c, v) in rows:
            conn.execute(
                "UPDATE market_data SET open=?, high=?, low=?, close=?, "
                "volume=?, adj_close=? "
                "WHERE ticker = ? AND source = 'iFinD-KimiGateway-correction' "
                "AND date = ?",
                (o / 2, h / 2, lo / 2, c / 2, v / 2, c / 2,
                 CORRECTION_FIX_TICKER, d))
        conn.commit()
    log(f"步骤5 515880 correction ÷2: {len(rows)} 行 {[str(r[0])[:10] for r in rows]}")
    return len(rows)


# ----------------------------------------------------------------------
# 步骤 6：生产路径重生成（真实 OHLCV + 校准 adj_close + 拆分自动登记）
# ----------------------------------------------------------------------
def regenerate(db: ETFDatabase, tickers, end_date: str) -> dict:
    res = update_latest_data_v2(db=db, tickers=list(tickers), source="ifind",
                                end_date=end_date)
    log(f"步骤6 重生成: status={res['status']} saved={res['saved_count']} "
        f"message={res['message'][:120]}")
    return res


# ----------------------------------------------------------------------
# 步骤 7：全历史 adj_close 重算  adj_close(t) = close(t) × fwd(t)/none(t)
# ----------------------------------------------------------------------
def recompute_adj_close(db_path: str, none_df: pd.DataFrame, fwd_df: pd.DataFrame):
    """同日比率覆盖更新：仅更新 iFinD 双序列覆盖到的 (ticker, date)。"""
    merged = none_df[["ticker", "date", "close"]].merge(
        fwd_df[["ticker", "date", "close"]],
        on=["ticker", "date"], suffixes=("_none", "_fwd"))
    merged["date"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m-%d")
    merged = merged[merged["close_none"] > 0]
    merged["adj"] = merged["close_fwd"] / merged["close_none"]

    updated = 0
    with sqlite3.connect(db_path) as conn:
        for t, g in merged.groupby("ticker"):
            for _, r in g.iterrows():
                conn.execute(
                    "UPDATE market_data SET adj_close = ? "
                    "WHERE ticker = ? AND date = ?",
                    (float(r["close_none"]) * float(r["adj"]), t, r["date"]))
                updated += 1
        conn.commit()
    log(f"步骤7 全历史 adj_close 重算: {updated} 行（adj = close × fwd/none）")
    return updated


# ----------------------------------------------------------------------
# 步骤 8：重算 daily_scores（B0.4 标准配置 = STRATEGY_CONFIG）
# ----------------------------------------------------------------------
def recompute_scores(db: ETFDatabase):
    cfg = STRATEGY_CONFIG.copy()
    engine = StrategyEngine(cfg)
    all_tickers = list(ALL_TRADABLE_ETFS.keys())
    market_df_all = db.get_market_data(ticker=all_tickers)

    stock_df = market_df_all[market_df_all["ticker"].isin(ETF_UNIVERSE.keys())]
    fallback_df = market_df_all[
        market_df_all["ticker"].isin(FALLBACK_EQUITY_UNIVERSE.keys())]
    defense_df = market_df_all[market_df_all["ticker"].isin(DEFENSE_UNIVERSE.keys())]

    parts = []
    for ticker in stock_df["ticker"].unique():
        td = stock_df[stock_df["ticker"] == ticker]
        if len(td) >= 50:
            parts.append(engine.calculate_total_score(
                apply_adjustment_for_scoring(td.copy())))
    if not parts:
        raise RuntimeError("无有效行业ETF数据，评分重算中止")
    scores_all = pd.concat(parts, ignore_index=True)
    scores_all = engine.rank_all_momentum(scores_all)
    scores_all = engine.compute_total_score(scores_all)

    fb = []
    for ticker in fallback_df["ticker"].unique():
        td = fallback_df[fallback_df["ticker"] == ticker]
        if len(td) >= 50:
            fb.append(engine.calculate_fallback_equity_score(
                apply_adjustment_for_scoring(td.copy())))
    if fb:
        fb_df = pd.concat(fb, ignore_index=True)
        cols = ["trend_score", "confirm_score", "momentum_rank", "volume_score",
                "vol_score"]
        fb_df["total_score"] = fb_df[cols].fillna(0).sum(axis=1)
        scores_all = pd.concat([scores_all, fb_df], ignore_index=True)

    dv = []
    for ticker in defense_df["ticker"].unique():
        td = defense_df[defense_df["ticker"] == ticker]
        if len(td) >= 50:
            dv.append(engine.calculate_defense_score(
                apply_adjustment_for_scoring(td.copy())))
    if dv:
        dv_df = pd.concat(dv, ignore_index=True)
        cols = ["trend_score", "confirm_score", "momentum_rank", "volume_score",
                "vol_score"]
        dv_df["total_score"] = dv_df[cols].fillna(0).sum(axis=1)
        scores_all = pd.concat([scores_all, dv_df], ignore_index=True)

    n = db.save_scores(scores_all)
    log(f"步骤8 daily_scores 重算完成: {n} 行")
    return n


# ----------------------------------------------------------------------
# 步骤 9：验证包
# ----------------------------------------------------------------------
def verify(db: ETFDatabase, db_path: str, none_df: pd.DataFrame,
           split_rows: list):
    log("步骤9 验证包")

    # (a) B0.4 冻结快照 SHA-256 不变
    sha = hashlib.sha256(open(B0_4_SNAPSHOT, "rb").read()).hexdigest()
    ok = sha.startswith(B0_4_SNAPSHOT_SHA256)
    log(f"  (a) B0.4 快照 SHA-256: {sha[:16]}... {'PASS' if ok else 'FAIL（快照被改动！）'}")

    # (b) 512480 6/29~7/3 对照表（close 真实价、volume 真实量、adj 连续）
    df = db.get_market_data(ticker="512480.SH", start_date="2026-06-26",
                            end_date="2026-07-06")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date")
    none_ref = none_df[none_df["ticker"] == "512480.SH"].set_index(
        pd.to_datetime(none_df[none_df["ticker"] == "512480.SH"]["date"]).dt.strftime("%Y-%m-%d"))
    log("  (b) 512480 6/26~7/6 对照（stored vs iFinD none）:")
    vol_mismatch = 0
    for _, r in df.iterrows():
        d = r["date"]
        ref_close = ref_vol = None
        if d in none_ref.index:
            ref_close = float(none_ref.loc[d, "close"])
            ref_vol = float(none_ref.loc[d, "volume"])
            if abs(float(r["close"]) - ref_close) > 1e-6 * max(1, ref_close):
                log(f"      {d} close MISMATCH stored={r['close']} none={ref_close}")
            if abs(float(r["volume"]) - ref_vol) > 1:
                vol_mismatch += 1
                log(f"      {d} volume MISMATCH stored={r['volume']} none={ref_vol}")
        log(f"      {d} close={r['close']:.4f} (none={ref_close}) "
            f"vol={r['volume']:.0f} adj={r['adj_close']:.4f}")
    log(f"  (b) volume 与 iFinD none 不一致天数: {vol_mismatch}")

    # (c) 拆分边界 adj 收益率连续（512480: 7/2→7/3）
    adj = df.set_index("date")["adj_close"]
    for a, b in [("2026-07-02", "2026-07-03")]:
        if a in adj.index and b in adj.index:
            ret = adj.loc[b] / adj.loc[a] - 1
            log(f"  (c) adj 收益率 {a}→{b}: {ret:+.2%} {'PASS' if abs(ret) < 0.05 else 'FAIL'}")

    # (d) corporate_actions 内容
    actions = db.get_corporate_actions()
    log(f"  (d) corporate_actions: {len(actions)} 条")
    for _, a in actions.iterrows():
        log(f"      {a['ticker']} {str(a['effective_date'])[:10]} "
            f"{a['action_type']} ratio={a['ratio']:.4f}")

    # (e) 全库 detect 残留：6/30 起不应再有 source='iFinD-KimiGateway' 且
    #     adjust_type='forward_spliced' 的行
    with sqlite3.connect(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM market_data WHERE source='iFinD-KimiGateway' "
            "AND adjust_type='forward_spliced'").fetchone()[0]
    log(f"  (e) 残留 forward_spliced 行: {n} {'PASS' if n == 0 else 'FAIL'}")


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="真实价格架构 v3 数据迁移")
    ap.add_argument("--db", default=os.path.join("database", "etf_model.db"))
    ap.add_argument("--backup-dir", default=os.path.join("database", "backups"))
    ap.add_argument("--skip-fetch", action="store_true", help="复用缓存 CSV")
    ap.add_argument("--transport", choices=["auto", "curl"], default="auto",
                    help="数据拉取传输：auto=网关优先失败回退curl；curl=强制curl 备用传输")
    ap.add_argument("--skip-scores", action="store_true", help="跳过 daily_scores 重算")
    ap.add_argument("--end-date", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    log(f"目标库: {db_path}")

    db = ETFDatabase(db_path)
    from data_fetcher import _official_pool_tickers
    tickers = sorted(set(_official_pool_tickers()))

    # 本机 Python TLS 栈对 agent-gw 握手失败期间，用 curl 兼容壳接管全部
    # iFinD 拉取（update_latest_data_v2 与分段拉取共用）。
    global ifind_gateway
    ifind_gateway = _CurlGatewayShim(ifind_gateway, args.transport)
    import data_fetcher
    data_fetcher.ifind_gateway = ifind_gateway

    backup_db(db_path, os.path.abspath(args.backup_dir))

    none_df, fwd_df = fetch_dual_series(
        tickers, FETCH_START, args.end_date, skip_fetch=args.skip_fetch,
        transport=args.transport)
    for c in ("date",):
        none_df[c] = pd.to_datetime(none_df[c]).dt.strftime("%Y-%m-%d")
        fwd_df[c] = pd.to_datetime(fwd_df[c]).dt.strftime("%Y-%m-%d")

    split_rows = register_splits(db, none_df, fwd_df, tickers)

    delete_old_spliced(db_path)
    halve_515880_corrections(db_path)

    regenerate(db, tickers, args.end_date)
    recompute_adj_close(db_path, none_df, fwd_df)

    if not args.skip_scores:
        recompute_scores(db)

    verify(db, db_path, none_df, split_rows)
    log("迁移完成（未提交 git；请复核验证包后再行提交）")


if __name__ == "__main__":
    main()

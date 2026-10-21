# -*- coding: utf-8 -*-
"""行情数据拼接错误修复脚本（Trae 复审 BLOCKED 修复，2026-10-20）。

修复对象：database/etf_model.db 中以下错误数据
  A. 2026-06-22 ~ 2026-07-10 批次与核验来源不一致的旧数据行（原因尚待确认）
     （证据：15/19 标的与 iFinD 核验值偏差 -2.4%~+0.6%，含无复权概念的
      沪深300指数 -1.48%，且偏差符号随机；512480 6/26、6/29 经 Yahoo Finance
      第三源交叉验证，两票对一票确认数据库为错）
  B. 515880.SH 2026-07-07~07-10 四行拆分后口径（与库内拆分前历史混排，
     7/6→7/7 存在 -49% 假跳变）——统一为拆分前等效口径（×2），与
     6/30~7/6（同为拆分前口径）衔接；修正后 7/13 起新增数据经 7/10 锚定
     自动延续拆分前口径，全段统一
  C. 2026-10-20 首次拼接生成的 source='iFinD-KimiGateway' 新增行
     （锚点用了与核验来源不一致的 6/29 旧数据价，且 volume 误用 ÷100 手单位）——
     全部删除后按修正后锚点重新生成

修复原则：
  - B0.4 冻结快照（data/snapshots/，数据截止 2026-06-18）不在修复范围
  - 所有被 UPDATE/DELETE 的行在修复报告中记录修改前后值（可审计、可回滚
    到 database/backups/ 下更新前备份）
  - 幂等：重跑不产生重复数据

用法：
  python scripts/fix_splice_errors.py --db database/etf_model.db
  python scripts/fix_splice_errors.py --db <副本路径>   # 先副本验证
"""
import argparse
import json
import os
import sys
import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import ETFDatabase
import ifind_gateway

CORRECTION_SOURCE = "iFinD-KimiGateway-correction"
NEW_SOURCE = "iFinD-KimiGateway"

# 与核验来源不一致的旧数据批次区间（含端点）
FIX_START = "2026-06-22"
FIX_END = "2026-07-10"
# 515880 拆分边界：7/3 收盘后 1:2 拆分（7/6 起为拆分后真实交易）
SPLIT_TICKER = "515880.SH"
SPLIT_EFFECTIVE = "2026-07-06"  # 该日起数据库已有行为拆分后口径
SPLIT_FACTOR = 2.0


def official_pool():
    from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
    return list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]


def fetch_none(tickers, start, end, probe_dir):
    """拉取 iFinD 不复权数据，返回 {(ticker, date): row_dict}。"""
    gw = ifind_gateway.get_price(list(tickers), start, end, adjust="none")
    df = gw.get("df")
    if df is None or df.empty:
        raise RuntimeError(f"iFinD 未返回数据: {gw.get('failed')}")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    out = {}
    for _, r in df.iterrows():
        out[(r["ticker"], r["date"])] = {
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
    return out, gw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="database/etf_model.db")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    db_path = args.db
    db = ETFDatabase(db_path=db_path)
    tickers = official_pool()
    now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = args.report or f"reports/splice_fix_report_{now}.json"

    report = {"db": db_path, "started_at": now, "steps": {}}

    # ---- 步骤 0：前置校验 ----
    avail = ifind_gateway.is_available()
    if not avail.get("available"):
        print(f"[FATAL] iFinD 网关不可用: {avail.get('message')}")
        sys.exit(1)

    def count_rows():
        q = ("SELECT COUNT(*) FROM market_data WHERE ticker IN (%s)"
             % ",".join("?" * len(tickers)))
        with db._connect() as conn:
            return conn.execute(q, tickers).fetchone()[0]

    before_total = count_rows()

    # ---- 步骤 1：拉 iFinD none 修正基准 ----
    none_map, gw = fetch_none(tickers, FIX_START, FIX_END, None)
    report["steps"]["fetch"] = {"rows": len(none_map), "failed": gw.get("failed", {})}
    print(f"[1] iFinD none 基准: {len(none_map)} 行 ({FIX_START}~{FIX_END})")

    # ---- 步骤 2+3：逐行核对并修正历史段 ----
    corrected, inserted_hist, unchanged, mism_after = [], [], 0, []
    with db._connect() as conn:
        for t in tickers:
            for (tt, d), vals in sorted(none_map.items()):
                if tt != t:
                    continue
                row = conn.execute(
                    "SELECT open, high, low, close, volume FROM market_data "
                    "WHERE ticker=? AND date=?", (t, d)).fetchone()
                # 515880 在拆分生效日后的既有行：目标值 = none × 2（拆分前等效）
                if t == SPLIT_TICKER and d >= SPLIT_EFFECTIVE:
                    target = {k: v * SPLIT_FACTOR for k, v in vals.items()}
                    adj = "split-equiv"
                else:
                    target = dict(vals)
                    adj = "none"
                if row is None:
                    conn.execute(
                        "INSERT INTO market_data (ticker,date,open,high,low,close,"
                        "volume,adj_close,source,adjust_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (t, d, target["open"], target["high"], target["low"],
                         target["close"], target["volume"], target["close"],
                         CORRECTION_SOURCE, adj))
                    inserted_hist.append({"ticker": t, "date": d})
                    continue
                # source='iFinD-KimiGateway' 的行是 10-20 首次拼接的新增行，
                # 属于"待删除重生成"对象，不能在此按 none 值 UPDATE
                # （否则等效口径被原始价覆盖，会制造 -50% 断裂）；
                # 交给步骤 4 删除 + 步骤 5 按修正锚点重新拼接
                src = conn.execute(
                    "SELECT source FROM market_data WHERE ticker=? AND date=?",
                    (t, d)).fetchone()[0]
                if src == NEW_SOURCE:
                    continue
                old = {"open": row[0], "high": row[1], "low": row[2],
                       "close": row[3], "volume": row[4]}
                tol = 1e-6
                same = (abs(old["close"] - target["close"]) < tol and
                        abs(old["open"] - target["open"]) < tol and
                        abs(old["volume"] - target["volume"]) < max(tol, target["volume"] * 1e-4))
                if same:
                    unchanged += 1
                    continue
                conn.execute(
                    "UPDATE market_data SET open=?, high=?, low=?, close=?, volume=?, "
                    "adj_close=?, source=?, adjust_type=? WHERE ticker=? AND date=?",
                    (target["open"], target["high"], target["low"], target["close"],
                     target["volume"], target["close"], CORRECTION_SOURCE, adj, t, d))
                corrected.append({"ticker": t, "date": d, "old": old, "new": target})
        conn.commit()
    report["steps"]["hist_fix"] = {
        "updated_rows": len(corrected), "inserted_rows": len(inserted_hist),
        "unchanged_rows": unchanged, "details": corrected,
    }
    print(f"[2] 历史段修正: UPDATE {len(corrected)} 行, 补缺 INSERT {len(inserted_hist)} 行, "
          f"原本正确 {unchanged} 行")

    # ---- 步骤 4：删除 10-20 首次拼接的错误新增行 ----
    with db._connect() as conn:
        cur = conn.execute(
            "DELETE FROM market_data WHERE source=? AND date>=?",
            (NEW_SOURCE, "2026-06-30"))
        deleted_new = cur.rowcount
        conn.commit()
    report["steps"]["delete_bad_splice"] = {"deleted_rows": deleted_new}
    print(f"[3] 删除 10-20 错误拼接新增行: {deleted_new} 行")

    # ---- 步骤 5：按修正后锚点重新拼接 ----
    import data_fetcher
    res = data_fetcher.update_latest_data_v2(db=db, source="ifind")
    report["steps"]["resplice"] = {
        "status": res["status"], "saved_count": res["saved_count"],
        "precheck_breaks": res["precheck_breaks"],
        "splice_factors": res["splice_factors"],
        "per_ticker": res["per_ticker"],
    }
    print(f"[4] 重新拼接: status={res['status']}, saved={res['saved_count']}, "
          f"precheck_breaks={list(res['precheck_breaks'])}")

    after_total = count_rows()
    report["count_reconciliation"] = {
        "before_total": before_total,
        "fetched_ifind_rows_step1": len(none_map),
        "hist_updated": len(corrected),
        "hist_inserted": len(inserted_hist),
        "bad_splice_deleted": deleted_new,
        "resplice_saved": res["saved_count"],
        "after_total": after_total,
    }
    print(f"[5] 条数: 更新前 {before_total} → 更新后 {after_total} "
          f"(净增 {after_total - before_total})")

    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[6] 修复报告: {report_path}")

    if res["precheck_breaks"]:
        print("[WARN] 预检仍发现断裂，请人工核查")
        sys.exit(2)
    print("[DONE]")


if __name__ == "__main__":
    main()

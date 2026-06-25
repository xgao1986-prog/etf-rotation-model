#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究数据：概念/主题 ETF 观察池数据更新

用法：
    py scripts/research_update_concept_etf_data.py --date 2026-06-26

说明：
    - 只更新研究数据，不进入 B0.4 交易逻辑
    - 从 etf_watch_universe.csv 读取观察池列表
    - 获取日线行情，保存到 concept_etf_daily.csv
    - 缺数据时只警告，不影响实盘
"""

import argparse, os, sys, warnings
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, "src")

DEFAULT_WATCH_UNIVERSE = "data/research/etf_watch_universe.csv"
DEFAULT_OUTPUT = "data/research/concept_etf_daily.csv"


def fetch_etf_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取单只 ETF 历史行情（复用 AKShare 逻辑）"""
    try:
        import akshare as ak
        df = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
        if df.empty:
            return pd.DataFrame()

        df = df.rename(columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        })
        df["date"] = pd.to_datetime(df["date"])
        df["ticker"] = f"{code}.SH" if code.startswith("5") else f"{code}.SZ"
        df["source"] = "AKShare"

        cols = ["ticker", "date", "open", "high", "low", "close", "volume", "amount", "source"]
        df = df[[c for c in cols if c in df.columns]]
        return df.sort_values("date").reset_index(drop=True)

    except Exception as e:
        warnings.warn(f"获取 ETF {code} 失败: {e}")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description="概念/主题 ETF 观察池数据更新（研究数据）")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="更新日期 (YYYY-MM-DD)")
    parser.add_argument("--start-date", type=str, default=None,
                        help="起始日期 (YYYY-MM-DD)，默认近30天")
    parser.add_argument("--watch-file", type=str, default=DEFAULT_WATCH_UNIVERSE,
                        help="观察池元数据 CSV 路径")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help="输出日线 CSV 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览，不写入文件")
    args = parser.parse_args()

    if args.start_date is None:
        args.start_date = (datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    print(f"研究数据：概念/主题 ETF 观察池数据更新")
    print(f"日期范围: {args.start_date} ~ {args.date}")
    print(f"观察池: {args.watch_file}")
    print(f"输出: {args.output}")
    print(f"{'[DRY-RUN]' if args.dry_run else ''}")
    print()

    # 读取观察池
    if not os.path.exists(args.watch_file):
        print(f"WARN 观察池文件不存在: {args.watch_file}")
        print(f"请先运行 scripts/research_scan_new_etfs.py 生成观察池，或手动创建 {args.watch_file}")
        return

    watch_df = pd.read_csv(args.watch_file)
    watch_df = watch_df[watch_df["status"].isin(["watch", "candidate"])]

    if watch_df.empty:
        print(f"WARN 观察池为空（无 watch/candidate 状态 ETF），跳过更新。")
        return

    print(f"观察池: {len(watch_df)} 只 ETF")
    print()

    all_rows = []
    success_count = 0
    fail_count = 0

    for _, row in watch_df.iterrows():
        ticker = row["ticker"]
        code = ticker.split(".")[0]  # 去掉 .SH/.SZ
        name = row.get("name", "")

        df = fetch_etf_history(code, args.start_date, args.date)
        if not df.empty:
            all_rows.append(df)
            success_count += 1
            print(f"  OK {ticker} {name}: {len(df)} 条")
        else:
            fail_count += 1
            print(f"  WARN {ticker} {name}: 无数据")

    if not all_rows:
        print(f"\nWARN 未获取到任何数据，跳过写入。")
        return

    combined = pd.concat(all_rows, ignore_index=True)
    combined["update_time"] = args.date

    print(f"\n汇总: {len(combined)} 条记录，来自 {success_count}/{len(watch_df)} 只 ETF")

    if args.dry_run:
        print(f"[DRY-RUN] 预览前 5 行:")
        print(combined.head().to_string(index=False))
    else:
        if os.path.exists(args.output):
            existing = pd.read_csv(args.output)
            existing["date"] = pd.to_datetime(existing["date"])
            combined["date"] = pd.to_datetime(combined["date"])
            combined = pd.concat([existing, combined], ignore_index=True)
            combined = combined.drop_duplicates(subset=["ticker", "date"], keep="last")

        combined.to_csv(args.output, index=False)
        print(f"OK 已保存: {args.output}")

    # 更新观察池的数据天数
    if not args.dry_run and os.path.exists(args.watch_file):
        watch_df = pd.read_csv(args.watch_file)
        for ticker in combined["ticker"].unique():
            days = len(combined[combined["ticker"] == ticker]["date"].unique())
            mask = watch_df["ticker"] == ticker
            if mask.any():
                watch_df.loc[mask, "data_days"] = days
                watch_df.loc[mask, "update_time"] = args.date
        watch_df.to_csv(args.watch_file, index=False)
        print(f"OK 已更新观察池数据天数: {args.watch_file}")

    print(f"\n声明: 概念/主题 ETF 数据目前仅用于研究观察，不进入B0.4交易逻辑。")


if __name__ == "__main__":
    main()

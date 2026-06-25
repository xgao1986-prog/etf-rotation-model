#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究数据：申万行业指数日线更新

用法：
    py scripts/research_update_industry_data.py --date 2026-06-26 --output data/research/industry_index_daily.csv

说明：
    - 只更新研究数据，不进入 B0.4 交易逻辑
    - 缺数据时只警告，不影响实盘
    - 支持 --dry-run 预览不写入

数据源：AKShare index_hist_sw
"""

import argparse, os, sys, warnings
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, "src")

# 申万一级行业指数代码列表（核心行业）
SW_LEVEL1_INDICES = {
    "801010": "农林牧渔",
    "801020": "采掘",
    "801030": "化工",
    "801040": "钢铁",
    "801050": "有色金属",
    "801060": "电子",
    "801070": "汽车",
    "801080": "家用电器",
    "801090": "食品饮料",
    "801100": "纺织服装",
    "801110": "轻工制造",
    "801120": "医药生物",
    "801130": "公用事业",
    "801140": "交通运输",
    "801150": "房地产",
    "801160": "商业贸易",
    "801170": "休闲服务",
    "801180": "综合",
    "801200": "建筑材料",
    "801210": "建筑装饰",
    "801230": "电气设备",
    "801240": "国防军工",
    "801250": "计算机",
    "801260": "传媒",
    "801270": "通信",
    "801280": "银行",
    "801290": "非银金融",
    "801300": "机械设备",
    "801310": "国防军工",
    "801710": "建筑材料",
    "801720": "建筑装饰",
    "801730": "电气设备",
    "801740": "国防军工",
    "801750": "计算机",
    "801760": "传媒",
    "801770": "通信",
    "801780": "银行",
    "801790": "非银金融",
    "801880": "汽车",
    "801890": "机械设备",
}


def fetch_sw_index_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取单只申万行业指数历史行情"""
    try:
        import akshare as ak
        df = ak.index_hist_sw(symbol=symbol, period="day")
        if df.empty:
            return pd.DataFrame()

        # 标准化列名（AKShare 返回中文列名）
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
        df["index_code"] = symbol
        df["index_name"] = SW_LEVEL1_INDICES.get(symbol, "")

        # 筛选日期范围
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]

        # 选择输出列
        cols = ["index_code", "index_name", "date", "open", "high", "low", "close", "volume", "amount"]
        df = df[[c for c in cols if c in df.columns]]
        return df.sort_values("date").reset_index(drop=True)

    except Exception as e:
        warnings.warn(f"获取申万行业指数 {symbol} 失败: {e}")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description="申万行业指数日线更新（研究数据）")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="更新日期 (YYYY-MM-DD)")
    parser.add_argument("--start-date", type=str, default=None,
                        help="起始日期 (YYYY-MM-DD)，默认近30天")
    parser.add_argument("--output", type=str, default="data/research/industry_index_daily.csv",
                        help="输出 CSV 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览，不写入文件")
    parser.add_argument("--max-indices", type=int, default=31,
                        help="最大更新指数数量，默认全部31只")
    args = parser.parse_args()

    if args.start_date is None:
        args.start_date = (datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    print(f"研究数据：申万行业指数日线更新")
    print(f"日期范围: {args.start_date} ~ {args.date}")
    print(f"输出: {args.output}")
    print(f"{'[DRY-RUN]' if args.dry_run else ''}")
    print()

    all_rows = []
    success_count = 0
    fail_count = 0

    indices = list(SW_LEVEL1_INDICES.items())[:args.max_indices]
    for code, name in indices:
        df = fetch_sw_index_history(code, args.start_date, args.date)
        if not df.empty:
            all_rows.append(df)
            success_count += 1
            print(f"  OK {code} {name}: {len(df)} 条")
        else:
            fail_count += 1
            print(f"  WARN {code} {name}: 无数据")

    if not all_rows:
        print(f"\nWARN 未获取到任何数据，跳过写入。")
        return

    combined = pd.concat(all_rows, ignore_index=True)
    combined["update_time"] = args.date

    print(f"\n汇总: {len(combined)} 条记录，来自 {success_count}/{len(indices)} 只指数")

    if args.dry_run:
        print(f"[DRY-RUN] 预览前 5 行:")
        print(combined.head().to_string(index=False))
    else:
        # 读取已有数据，追加并去重
        if os.path.exists(args.output):
            existing = pd.read_csv(args.output)
            existing["date"] = pd.to_datetime(existing["date"])
            combined["date"] = pd.to_datetime(combined["date"])
            combined = pd.concat([existing, combined], ignore_index=True)
            combined = combined.drop_duplicates(subset=["index_code", "date"], keep="last")

        combined.to_csv(args.output, index=False)
        print(f"OK 已保存: {args.output}")

    print(f"\n声明: 行业数据目前仅用于研究观察，不进入B0.4交易逻辑。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究数据：新 ETF 发现与观察池扫描

用法：
    py scripts/research_scan_new_etfs.py --min-listing-days 30 --output data/research/etf_watch_universe.csv

说明：
    - 扫描 AKShare 全部 ETF 列表，发现新上市 ETF
    - 判断是否与现有 B0.4 正式池重复
    - 如果是新主题，加入观察池
    - 不修改正式池，不进入交易逻辑
    - 生成观察报告 reports/research/universe_watch_report.md
"""

import argparse, os, sys, warnings
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, "src")
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, CONCEPT_UNIVERSE

DEFAULT_OUTPUT = "data/research/etf_watch_universe.csv"
DEFAULT_REPORT = "reports/research/universe_watch_report.md"


def get_all_etfs() -> pd.DataFrame:
    """获取全部 ETF 基础信息"""
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        if df.empty:
            return pd.DataFrame()

        # 标准化列名
        rename_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "last_price",
            "成交额": "turnover",
            "流通市值": "float_mv",
            "总市值": "total_mv",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        df["ticker"] = df["code"].apply(lambda x: f"{x}.SH" if str(x).startswith("5") else f"{x}.SZ")
        df["listing_date"] = pd.NaT  # AKShare 实时数据不含上市日期
        return df

    except Exception as e:
        warnings.warn(f"获取全部 ETF 列表失败: {e}")
        return pd.DataFrame()


def get_existing_tickers() -> set:
    """获取现有正式池的所有 ticker"""
    tickers = set()
    for universe in [ETF_UNIVERSE, DEFENSE_UNIVERSE, CONCEPT_UNIVERSE]:
        tickers.update(universe.keys())
    return tickers


def determine_status(row: pd.Series, existing_tickers: set) -> str:
    """判断新 ETF 的状态：duplicate / watch / candidate"""
    ticker = row.get("ticker", "")
    if ticker in existing_tickers:
        return "duplicate"

    # 简单判断：如果名称与现有 ETF 高度相似，也标记为 duplicate
    name = str(row.get("name", ""))
    for existing_name in list(ETF_UNIVERSE.values()) + list(DEFENSE_UNIVERSE.values()) + list(CONCEPT_UNIVERSE.values()):
        if existing_name in name or name in existing_name:
            return "duplicate"

    return "watch"


def main():
    parser = argparse.ArgumentParser(description="新 ETF 发现与观察池扫描（研究数据）")
    parser.add_argument("--min-listing-days", type=int, default=30,
                        help="最小上市天数，默认30天")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help="观察池 CSV 输出路径")
    parser.add_argument("--report", type=str, default=DEFAULT_REPORT,
                        help="观察报告 Markdown 输出路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览，不写入文件")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    os.makedirs(os.path.dirname(args.report), exist_ok=True)

    print(f"研究数据：新 ETF 发现与观察池扫描")
    print(f"{'[DRY-RUN]' if args.dry_run else ''}")
    print()

    # 获取全部 ETF
    all_etfs = get_all_etfs()
    if all_etfs.empty:
        print(f"WARN 未获取到 ETF 列表，跳过扫描。")
        return

    print(f"全部 ETF: {len(all_etfs)} 只")

    existing_tickers = get_existing_tickers()
    print(f"现有正式池: {len(existing_tickers)} 只")

    # 判断状态
    all_etfs["status"] = all_etfs.apply(lambda r: determine_status(r, existing_tickers), axis=1)

    # 筛选新发现（非 duplicate）
    new_etfs = all_etfs[all_etfs["status"] != "duplicate"].copy()
    print(f"新发现（非重复）: {len(new_etfs)} 只")
    print()

    # 构建观察池 DataFrame
    watch_rows = []
    for _, r in new_etfs.iterrows():
        watch_rows.append({
            "ticker": r["ticker"],
            "name": r.get("name", ""),
            "tracking_index": "",
            "theme_tag": "",
            "sector_mapping": "",
            "listing_date": "",
            "aum": r.get("total_mv", 0),
            "avg_volume": r.get("turnover", 0),
            "status": "watch",
            "data_days": 0,
            "notes": "新发现",
            "update_time": datetime.now().strftime("%Y-%m-%d"),
        })

    watch_df = pd.DataFrame(watch_rows)

    if args.dry_run:
        print(f"[DRY-RUN] 预览前 10 只新发现 ETF:")
        print(watch_df[["ticker", "name", "status", "aum", "avg_volume"]].head(10).to_string(index=False))
    else:
        # 合并已有观察池
        if os.path.exists(args.output):
            existing = pd.read_csv(args.output)
            combined = pd.concat([existing, watch_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["ticker"], keep="first")
        else:
            combined = watch_df

        combined.to_csv(args.output, index=False)
        print(f"OK 已保存观察池: {args.output} ({len(combined)} 只)")

    # 生成报告
    generate_report(new_etfs, existing_tickers, args.report, args.dry_run)
    print(f"\n声明: 新 ETF 数据目前仅用于研究观察，不进入B0.4交易逻辑。")


def generate_report(new_etfs: pd.DataFrame, existing_tickers: set, output_path: str, dry_run: bool):
    """生成观察报告 Markdown"""
    date = datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# ETF 观察池扫描报告 ({date})")
    lines.append("")
    lines.append("> **研究数据扩展层 v0.1** — 仅用于研究观察，不进入B0.4交易逻辑。")
    lines.append("")

    lines.append("## 1. 扫描概况")
    lines.append("")
    lines.append(f"- 扫描日期: {date}")
    lines.append(f"- 现有正式池: {len(existing_tickers)} 只 ETF")
    lines.append(f"- 新发现（非重复）: {len(new_etfs)} 只 ETF")
    lines.append("")

    if not new_etfs.empty:
        lines.append("## 2. 新发现 ETF 列表")
        lines.append("")
        lines.append("| ticker | 名称 | 最新价 | 成交额 | 状态 | 说明 |")
        lines.append("|--------|------|--------|--------|------|------|")
        for _, r in new_etfs.head(50).iterrows():
            price = r.get("last_price", 0)
            turnover = r.get("turnover", 0)
            lines.append(
                f"| {r['ticker']} | {r.get('name', '')} | {price:.3f} | {turnover:,.0f} | watch | 新发现，需进一步观察 |"
            )
        lines.append("")
    else:
        lines.append("## 2. 新发现 ETF 列表")
        lines.append("")
        lines.append("本次扫描未发现新 ETF。")
        lines.append("")

    lines.append("## 3. 重复判断")
    lines.append("")
    lines.append(f"- 与现有正式池重复的 ETF 已自动排除（{len(new_etfs)} 只保留）")
    lines.append("- 重复判断基于 ticker 和名称匹配")
    lines.append("")

    lines.append("## 4. 下一步建议")
    lines.append("")
    lines.append("1. 对新发现 ETF 运行 `scripts/research_update_concept_etf_data.py` 获取历史数据")
    lines.append("2. 收集至少 3 个月数据后评估夏普、收益、回撤")
    lines.append("3. 如果表现优于 B0.4 基线，可标记为 candidate")
    lines.append("4. 用户确认后，修改 config.py 正式池并重新回测")
    lines.append("")

    lines.append("## 5. 声明")
    lines.append("")
    lines.append("> **行业/概念ETF数据目前仅用于研究观察，不进入B0.4交易逻辑。**")
    lines.append("> 任何研究数据的纳入都必须经过用户确认、回测验证和版本记录。")
    lines.append("")

    content = "\n".join(lines)

    if dry_run:
        print(f"[DRY-RUN] 报告预览（前 20 行）:")
        print("\n".join(lines[:20]))
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"OK 报告已保存: {output_path}")


if __name__ == "__main__":
    main()

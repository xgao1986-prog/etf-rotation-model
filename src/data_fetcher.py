"""
数据获取模块 - 本地AKShare + 云端iFinD桥接

工作流:
  1. 本地自动: AKShare获取（免费，无需账号）
  2. 云端桥接: iFinD数据通过Kimi对话获取，导入本地数据库
  3. 数据合并: iFinD为主，AKShare补充缺失部分

使用方式:
  # 本地自动获取（AKShare）
  from data_fetcher import download_all_data
  download_all_data('2019-06-03', db=db)

  # 从Kimi导入iFinD数据（在Kimi对话中执行）
  from data_fetcher import import_from_kimi
  import_from_kimi(ifind_df, db=db, source_label='iFinD')
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from config import (ETF_UNIVERSE, BENCHMARK, ETF_CODES, BENCHMARK_CODE,
                    FALLBACK_EQUITY_UNIVERSE, DEFENSE_UNIVERSE, ALL_TRADABLE_ETFS)

# v1.2板块数据导入（已注释，未来使用）
try:
    from config import SECTOR_CODES, SECTOR_INDEX_UNIVERSE, ETF_TO_SECTOR_MAPPING
except ImportError:
    SECTOR_CODES = []
    SECTOR_INDEX_UNIVERSE = {}
    ETF_TO_SECTOR_MAPPING = {}

# 宽基补仓ETF代码（不带后缀）
FALLBACK_EQUITY_CODES = [code.split('.')[0] for code in FALLBACK_EQUITY_UNIVERSE.keys()]
# 防御资产代码（不带后缀）
DEFENSE_CODES = [code.split('.')[0] for code in DEFENSE_UNIVERSE.keys()]
# 所有可交易ETF代码
ALL_ETF_CODES = [code.split('.')[0] for code in ALL_TRADABLE_ETFS.keys()]


class DataFetcher:
    """本地数据获取器 - 优先AKShare，支持失败fallback"""

    def __init__(self, timeout=3, max_retries=1):
        self._ak = None  # 懒加载AKShare
        self.timeout = timeout
        self.max_retries = max_retries
        self.failed_tickers = []  # 记录本次失败ticker
        self.success_tickers = []  # 记录本次成功ticker

    def _get_akshare(self):
        """懒加载AKShare，未安装时给出明确提示"""
        if self._ak is None:
            try:
                import akshare as ak
                self._ak = ak
            except ImportError:
                raise ImportError(
                    "AKShare 未安装。请运行：pip install akshare\n"
                    "或继续使用 iFinD CSV 导入方式更新数据。"
                )
        return self._ak

    def _fetch_with_retry(self, fetch_func, code, label=""):
        """带外部超时和指数退避重试的获取辅助函数。

        由于 AKShare 内部 requests 超时参数不一定生效，使用 ThreadPoolExecutor
        设置硬超时，避免单只 ticker 卡住十几秒。

        Parameters:
            fetch_func: 实际调用的获取函数，返回DataFrame
            code: ticker代码
            label: 用于日志的类别标签

        Returns:
            DataFrame
        """
        last_error = None
        for attempt in range(self.max_retries):
            try:
                df = self._fetch_with_timeout(fetch_func, self.timeout)
                if not df.empty and code not in self.success_tickers:
                    self.success_tickers.append(code)
                return df
            except Exception as e:
                last_error = e
                # 只有还有下一次尝试时才等待
                if attempt < self.max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    warnings.warn(f"{label}{code} 第 {attempt + 1}/{self.max_retries} 次请求失败: {e}，{wait}s 后重试")
                    time.sleep(wait)

        if code not in self.failed_tickers:
            self.failed_tickers.append(code)
        warnings.warn(f"{label}{code} 最终失败: {last_error}")
        return pd.DataFrame()

    def _fetch_with_timeout(self, fetch_func, timeout):
        """在线程池中执行 fetch_func，强制外部超时，超时不等待后台线程。"""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fetch_func)
        try:
            result = future.result(timeout=timeout)
            executor.shutdown(wait=False)
            return result
        except FutureTimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"请求超过 {timeout} 秒未返回")

    def fetch_etf_history(self, code, start_date, end_date=None, adjust='qfq') -> pd.DataFrame:
        """获取单只ETF历史行情（带重试）"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        ak = self._get_akshare()

        def _fetch():
            # 部分akshare版本支持timeout参数
            try:
                df = ak.fund_etf_hist_em(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    adjust=adjust,
                    timeout=self.timeout,
                )
            except TypeError:
                # 旧版本不支持timeout
                df = ak.fund_etf_hist_em(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    adjust=adjust,
                )

            if df.empty:
                return pd.DataFrame()

            df = df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
            })

            df['date'] = pd.to_datetime(df['date'])
            df['ticker'] = f"{code}.SH" if code.startswith('5') else f"{code}.SZ"
            df['adj_close'] = df['close']
            df['source'] = 'AKShare'
            df['adjust_type'] = 'qfq'  # 前复权

            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close', 'source', 'adjust_type']
            df = df[[c for c in cols if c in df.columns]]

            return df.sort_values('date').reset_index(drop=True)

        return self._fetch_with_retry(_fetch, code, label="AKShare获取ETF ")

    def fetch_index_history(self, code, start_date, end_date=None) -> pd.DataFrame:
        """获取指数历史行情（带重试）"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        ak = self._get_akshare()

        def _fetch():
            try:
                df = ak.index_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    timeout=self.timeout,
                )
            except TypeError:
                df = ak.index_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                )

            if df.empty:
                return pd.DataFrame()

            df = df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
            })

            df['date'] = pd.to_datetime(df['date'])
            df['ticker'] = f"{code}.SH"
            df['adj_close'] = df['close']
            df['source'] = 'AKShare'

            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close', 'source']
            df = df[[c for c in cols if c in df.columns]]

            return df.sort_values('date').reset_index(drop=True)

        return self._fetch_with_retry(_fetch, code, label="AKShare获取指数 ")

    def health_check(self, test_code='512480') -> dict:
        """快速检查 AKShare 接口是否可用。

        先请求一只常见 ETF 的最新一天数据，如果失败则判断接口当前不可用，
        避免对所有 ticker 都进行长时间超时等待。

        Returns:
            dict: {"ok": bool, "elapsed": float, "message": str}
        """
        import time as _time
        start = _time.time()
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            df = self.fetch_etf_history(test_code, today, today)
            elapsed = _time.time() - start
            if df.empty:
                return {
                    "ok": False,
                    "elapsed": elapsed,
                    "message": f"AKShare 接口预检失败：{test_code} 返回空数据（耗时 {elapsed:.1f}s）",
                }
            return {
                "ok": True,
                "elapsed": elapsed,
                "message": f"AKShare 接口正常（{test_code}，耗时 {elapsed:.1f}s）",
            }
        except Exception as e:
            elapsed = _time.time() - start
            return {
                "ok": False,
                "elapsed": elapsed,
                "message": f"AKShare 接口预检失败：{e}（耗时 {elapsed:.1f}s）",
            }

    def fetch_sector_history(self, code, start_date, end_date=None) -> pd.DataFrame:
        """获取申万一级行业指数历史行情（板块动量增强数据源）

        Parameters:
            code: 申万行业代码（如 '801080'）
            start_date: 开始日期 'YYYY-MM-DD'
            end_date: 结束日期 'YYYY-MM-DD'（默认今天）

        Returns:
            标准化后的DataFrame（列与ETF数据一致）
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        try:
            ak = self._get_akshare()

            df = ak.index_hist_sw(
                symbol=code,
                period='day'
            )

            if df.empty:
                return pd.DataFrame()

            # 申万指数数据列名处理：不同板块返回的列名格式可能不同
            # 检查是否是8列（含代码列）或7列（标准格式）
            if len(df.columns) == 8:
                # 8列格式：第一列可能是代码列，检查并跳过
                first_col_vals = df.iloc[:, 0].astype(str).unique()[:5]
                if code in first_col_vals or any(code in str(v) for v in first_col_vals):
                    df = df.iloc[:, 1:]  # 跳过代码列

            # 统一用位置索引重命名（7列标准格式）
            if len(df.columns) >= 7:
                df.columns = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount'] + list(df.columns[7:])

            # 如果位置索引失败，尝试用中文字符重命名
            if 'date' not in df.columns:
                df = df.rename(columns={
                    '日期': 'date',
                    '开盘': 'open',
                    '收盘': 'close',
                    '最高': 'high',
                    '最低': 'low',
                    '成交量': 'volume',
                    '成交额': 'amount',
                })

            df['date'] = pd.to_datetime(df['date'])

            # 过滤日期范围
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]

            if df.empty:
                return pd.DataFrame()

            # 使用板块指数代码作为ticker（加前缀区分）
            df['ticker'] = f"SECTOR_{code}"
            df['adj_close'] = df['close']
            df['source'] = 'AKShare-Sector'

            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close', 'source']
            df = df[[c for c in cols if c in df.columns]]

            return df.sort_values('date').reset_index(drop=True)

        except Exception as e:
            warnings.warn(f"AKShare获取板块指数 {code} 失败: {e}")
            return pd.DataFrame()

    def fetch_all_sectors(self, start_date, end_date=None, delay=0.5) -> pd.DataFrame:
        """批量获取所有板块指数数据"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        all_data = []

        for code in SECTOR_CODES:
            df = self.fetch_sector_history(code, start_date, end_date)
            if not df.empty:
                all_data.append(df)
            time.sleep(delay)

        if not all_data:
            return pd.DataFrame()

        return pd.concat(all_data, ignore_index=True)

    def fetch_all_data(self, start_date, end_date=None, db=None, delay=0.5) -> pd.DataFrame:
        """批量获取所有数据：ETF + 宽基补仓 + 防御资产 + 基准 + 板块指数"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        # 重置本次获取的成功/失败记录
        self.success_tickers = []
        self.failed_tickers = []

        all_data = []

        # 获取行业ETF数据
        for code in ETF_CODES:
            df = self.fetch_etf_history(code, start_date, end_date)
            if not df.empty:
                all_data.append(df)
            time.sleep(delay)

        # 获取宽基补仓ETF数据
        for code in FALLBACK_EQUITY_CODES:
            df = self.fetch_etf_history(code, start_date, end_date)
            if not df.empty:
                all_data.append(df)
            time.sleep(delay)

        # 获取防御资产数据（黄金/国债）
        for code in DEFENSE_CODES:
            df = self.fetch_etf_history(code, start_date, end_date)
            if not df.empty:
                all_data.append(df)
            time.sleep(delay)

        # 获取基准数据
        bench_df = self.fetch_index_history(BENCHMARK_CODE, start_date, end_date)
        if not bench_df.empty:
            all_data.append(bench_df)

        # 获取板块指数数据
        sector_df = self.fetch_all_sectors(start_date, end_date, delay=delay)
        if not sector_df.empty:
            all_data.append(sector_df)

        if not all_data:
            return pd.DataFrame()

        return pd.concat(all_data, ignore_index=True)

    def fetch_latest(self, db=None) -> pd.DataFrame:
        """获取最新一天的数据（增量更新）。

        只拉取数据库最新日期次日到今天的数据，避免每次请求过宽时间窗口。
        先进行健康预检，接口不可用则快速返回空表，避免长时间等待所有 ticker 超时。
        """
        today = datetime.now().strftime('%Y-%m-%d')

        if db:
            latest = db.get_latest_date()
            if latest:
                # 从最新日期的次日开始，避免重复拉取已存在数据
                start = (pd.to_datetime(latest) + timedelta(days=1)).strftime('%Y-%m-%d')
            else:
                start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        else:
            start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')

        # 如果 start 已经超过 today，说明数据已经是最新
        if pd.to_datetime(start) > pd.to_datetime(today):
            print(f"数据库已是最新（{latest}），无需更新")
            return pd.DataFrame()

        # 健康预检：接口不可用则快速失败
        check = self.health_check()
        print(check["message"])
        if not check["ok"]:
            # 预检失败时记录一个失败 ticker，让上层知道 AKShare 不可用
            if 'HEALTH_CHECK' not in self.failed_tickers:
                self.failed_tickers.append('HEALTH_CHECK')
            return pd.DataFrame()

        return self.fetch_all_data(start, today, db=db, delay=0.3)


# ==================== iFinD桥接函数 ====================

def import_from_kimi(df: pd.DataFrame, db=None, source_label='iFinD') -> int:
    """
    从Kimi对话导入iFinD数据到本地数据库

    使用场景:
      在Kimi对话中，我通过get_data_source工具获取iFinD数据后，
      调用此函数将数据写入本地数据库。

    Parameters:
        df: iFinD返回的DataFrame（需包含ticker, date, close等列）
        db: ETFDatabase实例
        source_label: 数据源标记

    Returns:
        导入的记录数

    Example:
        # 在Kimi对话中:
        # 1. 我用工具获取iFinD数据 -> ifind_df
        # 2. 调用 import_from_kimi(ifind_df, db=db)
    """
    if df.empty:
        print("导入数据为空，跳过")
        return 0

    # 标准化列名（处理iFinD可能的列名差异）
    column_mapping = {
        'thscode': 'ticker',
        'time': 'date',
        'TIME': 'date',
        'OPEN': 'open',
        'HIGH': 'high',
        'LOW': 'low',
        'CLOSE': 'close',
        'VOLUME': 'volume',
        'AMOUNT': 'amount',
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'volume': 'volume',
        'amount': 'amount',
    }

    df = df.copy()

    # 重命名列
    rename_map = {}
    for old_col, new_col in column_mapping.items():
        if old_col in df.columns and new_col not in df.columns:
            rename_map[old_col] = new_col

    if rename_map:
        df = df.rename(columns=rename_map)

    # 确保必要列存在
    required_cols = ['ticker', 'date', 'close']
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"导入数据缺少必要列: {col}。现有列: {list(df.columns)}")

    # 日期格式转换
    df['date'] = pd.to_datetime(df['date'])

    # 数值转换
    numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # 添加adj_close、source和adjust_type标记
    df['adj_close'] = df['close']
    df['source'] = source_label
    df['adjust_type'] = 'forward'  # iFinD默认前复权

    # 选择标准列
    cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close', 'source', 'adjust_type']
    df = df[[c for c in cols if c in df.columns]]

    # 保存到数据库
    if db is not None:
        count = db.save_market_data(df)
        print(f"已导入 {count} 条 {source_label} 记录到数据库")
        return count

    print(f"数据已标准化，共 {len(df)} 条（未保存到数据库，db参数为None）")
    return len(df)


def import_local_ifind_csvs(db=None, csv_dir='data', pattern='ifind_*.csv', source_label='iFinD'):
    """自动扫描并导入本地 iFinD CSV 文件。

    Parameters:
        db: ETFDatabase实例
        csv_dir: 扫描目录（相对于工作目录）
        pattern: 文件匹配模式
        source_label: 数据源标记

    Returns:
        dict: {
            "total_count": int,      # 总导入记录数
            "files": list,           # 每个文件导入详情
            "status": str,           # "success" | "no_files" | "partial" | "error"
            "message": str,          # 人类可读说明
        }
    """
    import glob
    import os

    result = {
        "total_count": 0,
        "files": [],
        "status": "no_files",
        "message": "未找到本地 iFinD CSV 文件",
    }

    search_path = os.path.join(csv_dir, pattern)
    files = sorted(glob.glob(search_path))

    if not files:
        return result

    all_success = True
    for file_path in files:
        file_name = os.path.basename(file_path)
        try:
            df = pd.read_csv(file_path)
            count = import_from_kimi(df, db=db, source_label=source_label)
            result["files"].append({
                "file": file_name,
                "count": count,
                "status": "success",
            })
            result["total_count"] += count
        except Exception as e:
            all_success = False
            result["files"].append({
                "file": file_name,
                "count": 0,
                "status": "error",
                "error": str(e),
            })
            warnings.warn(f"导入 iFinD CSV 失败 {file_name}: {e}")

    if result["total_count"] > 0 and all_success:
        result["status"] = "success"
        result["message"] = f"成功导入 {len(files)} 个 iFinD CSV 文件，共 {result['total_count']} 条记录"
    elif result["total_count"] > 0:
        result["status"] = "partial"
        result["message"] = f"部分导入成功，共 {result['total_count']} 条记录，部分文件失败"
    else:
        result["status"] = "error"
        result["message"] = "所有 iFinD CSV 文件导入失败，请检查文件格式"

    return result


def merge_data_sources(ifind_df: pd.DataFrame, akshare_df: pd.DataFrame,
                       priority='iFinD') -> pd.DataFrame:
    """
    合并iFinD和AKShare数据，处理重复

    Parameters:
        ifind_df: iFinD数据
        akshare_df: AKShare数据
        priority: 优先使用哪个数据源的数据

    Returns:
        合并后的DataFrame
    """
    # 标记数据源
    if not ifind_df.empty:
        ifind_df = ifind_df.copy()
        ifind_df['source'] = 'iFinD'

    if not akshare_df.empty:
        akshare_df = akshare_df.copy()
        akshare_df['source'] = 'AKShare'

    # 合并
    if ifind_df.empty and akshare_df.empty:
        return pd.DataFrame()
    elif ifind_df.empty:
        return akshare_df
    elif akshare_df.empty:
        return ifind_df

    combined = pd.concat([ifind_df, akshare_df], ignore_index=True)

    # 去重：按ticker+date，优先保留priority指定的数据源
    combined = combined.sort_values('source', key=lambda x: x != priority)
    combined = combined.drop_duplicates(subset=['ticker', 'date'], keep='first')

    return combined.sort_values(['ticker', 'date']).reset_index(drop=True)


# ==================== 便捷函数 ====================

def download_all_data(start_date='2019-06-03', end_date=None, db=None, include_sectors=True):
    """
    通过AKShare下载所有数据并保存到数据库

    v1.2.2: 导入前自动备份数据库

    Parameters:
        include_sectors: 是否同时下载板块指数数据（默认True）

    注意: 此函数仅使用AKShare。如需iFinD数据，
          请在Kimi对话中说"用iFinD获取数据"，
          我会调用工具获取并导入。
    """
    # 导入前自动备份
    if db is not None:
        try:
            import backup_database
            backup_database.backup_database()
            print("✅ 数据库已备份")
        except Exception as e:
            print(f"⚠️ 备份失败（继续导入）: {e}")

    try:
        fetcher = DataFetcher()
    except ImportError as e:
        print(f"[ERROR] {e}")
        print("[提示] 请安装 AKShare: pip install akshare")
        return pd.DataFrame()

    print(f"="*60)
    print(f"AKShare数据下载: {start_date} ~ {end_date or '今天'}")
    print(f"="*60)

    # 获取ETF+基准数据
    df = fetcher.fetch_all_data(start_date, end_date, delay=0.5)

    if df.empty:
        print("\n⚠️ 下载失败，无数据")
        return pd.DataFrame()

    # 分离ETF/基准数据和板块数据
    etf_df = df[~df['ticker'].str.startswith('SECTOR_')].copy()
    sector_df = df[df['ticker'].str.startswith('SECTOR_')].copy()

    # 保存到数据库
    if db is not None:
        if not etf_df.empty:
            etf_count = db.save_market_data(etf_df)
            print(f"已保存 {etf_count} 条 ETF/基准 记录到数据库")

        if not sector_df.empty:
            sector_count = db.save_sector_data(sector_df)
            print(f"已保存 {sector_count} 条 板块指数 记录到数据库")

    print(f"\n下载完成: {len(df)} 条记录")
    print(f"  - ETF/基准: {len(etf_df)} 条, {etf_df['ticker'].nunique()} 只标的")
    if not sector_df.empty:
        print(f"  - 板块指数: {len(sector_df)} 条, {sector_df['ticker'].nunique()} 个板块")
    print(f"日期范围: {df['date'].min().date()} ~ {df['date'].max().date()}")

    return df


def update_latest_data(db=None):
    """增量更新最新数据（AKShare），返回包含保存条数与失败详情的字典。

    返回字典格式：
    {
        "saved_count": int,          # 实际保存到数据库的记录数
        "fetched_count": int,        # 从AKShare抓取的记录数
        "success_tickers": list,     # 成功获取的ticker列表
        "failed_tickers": list,      # 失败（含重试后仍失败）的ticker列表
        "status": str,               # "no_data" | "success" | "partial" | "akshare_not_installed"
        "message": str,              # 人类可读的状态说明
    }

    v1.2.2: 更新前自动备份数据库
    """
    result = {
        "saved_count": 0,
        "fetched_count": 0,
        "success_tickers": [],
        "failed_tickers": [],
        "status": "no_data",
        "message": "无新数据",
    }

    # 更新前自动备份
    if db is not None:
        try:
            import backup_database
            backup_database.backup_database()
            print("[OK] 数据库已备份")
        except Exception as e:
            print(f"[WARN] 备份失败（继续更新）: {e}")

    try:
        fetcher = DataFetcher()
    except ImportError as e:
        result["status"] = "akshare_not_installed"
        result["message"] = (
            f"AKShare 未安装: {e}\n"
            "请安装 AKShare: pip install akshare\n"
            "或改用 iFinD CSV 导入方式。"
        )
        print(f"[ERROR] {result['message']}")
        return result

    print(f"AKShare增量更新...")
    df = fetcher.fetch_latest(db=db)

    result["success_tickers"] = fetcher.success_tickers
    result["failed_tickers"] = fetcher.failed_tickers

    if df.empty:
        if 'HEALTH_CHECK' in fetcher.failed_tickers:
            result["status"] = "no_data"
            result["message"] = (
                "AKShare 接口当前不可用（健康预检失败），可能是网络或东方财富接口限速。"
                "建议使用「自动导入本地 iFinD CSV」或「手动上传 iFinD CSV」补齐数据。"
            )
        elif fetcher.failed_tickers:
            result["status"] = "no_data"
            result["message"] = (
                f"AKShare 请求全部失败（共 {len(fetcher.failed_tickers)} 只标的超时/异常）。"
                "建议使用 iFinD CSV 导入方式补齐数据。"
            )
        else:
            result["message"] = "数据库已是最新，无需更新"
        print(result["message"])
        return result

    result["fetched_count"] = len(df)

    if db is not None:
        count = db.save_market_data(df)
        result["saved_count"] = count
    else:
        result["saved_count"] = len(df)

    if fetcher.failed_tickers:
        result["status"] = "partial"
        result["message"] = (
            f"部分更新成功：保存 {result['saved_count']} 条记录，"
            f"成功 {len(fetcher.success_tickers)} 只标的，"
            f"失败 {len(fetcher.failed_tickers)} 只标的（{', '.join(fetcher.failed_tickers[:5])}"
            f"{' 等' if len(fetcher.failed_tickers) > 5 else ''}）。"
        )
    else:
        result["status"] = "success"
        result["message"] = f"AKShare 更新成功，保存 {result['saved_count']} 条记录"

    print(result["message"])
    return result


def get_data_summary(df: pd.DataFrame) -> dict:
    """获取数据摘要"""
    if df.empty:
        return {'status': 'empty'}

    sources = df['source'].value_counts().to_dict() if 'source' in df.columns else {}

    return {
        'status': 'ok',
        'total_rows': len(df),
        'tickers': df['ticker'].nunique(),
        'date_range': (df['date'].min().strftime('%Y-%m-%d'),
                      df['date'].max().strftime('%Y-%m-%d')),
        'trading_days': df['date'].nunique(),
        'sources': sources,
    }


if __name__ == '__main__':
    print("测试数据获取器...")
    fetcher = DataFetcher()

    # 测试单只ETF
    df = fetcher.fetch_etf_history('512480', '2024-01-01')
    print(f"\n512480数据: {len(df)} 条")
    if not df.empty:
        print(df.head(3))

    # 测试指数
    bench = fetcher.fetch_index_history('000300', '2024-01-01')
    print(f"\n沪深300数据: {len(bench)} 条")
    if not bench.empty:
        print(bench.head(3))

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

# 行情更新修复 v2：iFinD 网关（subprocess 调 Kimi 插件），import 失败时降级为 None
try:
    import ifind_gateway
except ImportError:
    ifind_gateway = None

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


# ==================== 行情更新修复 v2（iFinD 就近锚定拼接） ====================
# 设计文档: docs/DATA_UPDATE_FIX_v2_DESIGN.md 第 2.3 / 3.2 节
#
# 核心算法（就近锚定因子法）:
#   1. 对每只正式标的，取数据库全部已有 (date, close)
#   2. 从 iFinD 拉统一区间 [min(各标的最后已有日)-10天, end_date] 的 forward 序列
#      （iFinD forward 全历史统一复权，拆分已在序列内部处理）
#   3. 交易日历 = iFinD 000300.SH 的日期序列
#   4. 缺失日 = 交易日历 ∩ (首条数据日, end_date] − 已有日期
#   5. 每个连续缺失段的锚定日 A = 数据库中 < 段首的最近已有日
#      factor = close_db(A) / fwd_close(A)；等效 OHLC = fwd × factor；volume = iFinD volume / 100
#   6. source='iFinD-KimiGateway'，adjust_type='forward_spliced'（如实标记非标准复权口径）
#
# 数学性质: factor 在拆分边界自动吸收拆分比例（fwd 全历史统一复权），
# 跨拆分拼接无假跳变；因子随锚定日分段更新，可处理"库内已有断裂+区间内缺失"。

IFIND_SOURCE_LABEL = 'iFinD-KimiGateway'
IFIND_ADJUST_TYPE = 'forward_spliced'


def _official_pool_tickers():
    """正式池：16 只行业 ETF + 防御资产 + 沪深300基准。"""
    return list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]


def detect_splice_breaks(db, tickers=None, threshold=0.35):
    """库内口径断裂预检：扫描日收益率绝对值超过 threshold 的可疑跳变。

    用途：在行情更新前发现"拆分/除权前后口径混排"类历史数据错误
    （如 515880 在 2026-07 的 1:2 拆分曾造成 -49% 假跳变），
    防止在断裂数据上拼接新数据并误报"全部成功"。

    真实价格架构 v3：corporate_actions 已登记的真实拆分日
    （effective_date）会被排除——真实拆分造成的 -50% 是真实公司行动，
    不属于口径断裂；未登记的 -49% 跳变仍会被报出。

    Parameters:
        db: ETFDatabase 实例
        tickers: 标的列表，默认正式池
        threshold: 日收益率绝对值阈值（默认 0.35=35%）。
                   ETF 涨跌停约 10%，超过 35% 基本可判定为口径断裂而非真实行情。

    Returns:
        dict: {ticker: [{"date", "prev_date", "prev_close", "close", "return"}], ...}
              无断裂返回空 dict。
    """
    if tickers is None:
        tickers = _official_pool_tickers()

    # 已登记的真实拆分日：{ticker: [effective_date, ...]}
    known_splits = {}
    if db is not None:
        try:
            ca = db.get_corporate_actions()
            if ca is not None and not ca.empty:
                for _, r in ca.iterrows():
                    known_splits.setdefault(r["ticker"], []).append(
                        r["effective_date"])
        except Exception:
            known_splits = {}

    def _near_known_split(ticker, date_str, window_days=7):
        """跳变日是否落在某条已登记拆分日的 window_days 自然日窗口内。

        拆分日前后若恰好缺失若干交易日（如迁移中删行重建的空档），
        拆分跳变会显现为空档后第一个交易日，与登记的 effective_date
        错开，精确匹配会漏排。窗口匹配按自然日容差，避免误报。
        """
        dates = known_splits.get(ticker) or []
        if not dates:
            return False
        d = pd.Timestamp(date_str)
        for eff in dates:
            try:
                e = pd.Timestamp(str(eff)[:10])
            except Exception:
                continue
            if pd.Timedelta(0) <= (d - e) <= pd.Timedelta(days=window_days):
                return True
        return False

    breaks = {}
    for t in tickers:
        try:
            df_t = db.get_market_data(ticker=t)
        except Exception:
            continue
        if df_t is None or len(df_t) < 2:
            continue
        d = df_t[["date", "close"]].copy()
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        d = d.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        prev_close = d["close"].shift(1)
        ret = d["close"] / prev_close - 1
        mask = ret.abs() > threshold
        if mask.any():
            items = []
            for idx in d.index[mask]:
                date = d.loc[idx, "date"]
                if _near_known_split(t, date):
                    continue  # 真实拆分日（含拆后短窗口），非口径断裂
                items.append({
                    "date": date,
                    "prev_date": d.loc[idx - 1, "date"],
                    "prev_close": round(float(prev_close.loc[idx]), 6),
                    "close": round(float(d.loc[idx, "close"]), 6),
                    "return": round(float(ret.loc[idx]), 4),
                })
            if items:
                breaks[t] = items
    return breaks


def _detect_splits_from_none_fwd(ticker, none_df, fwd_df):
    """从 iFinD none/fwd 双序列检测拆分事件（真实价格架构 v3，设计文档 2.3 节）。

    原理：无公司行动时 none_close/fwd_close 比率恒定；1:2 拆分在拆分日后
    比率降为一半（fwd 已把拆前价格折半）。比率阶跃 >20% 判定为拆分，
    effective_date = 新比率首个交易日，ratio = 旧比率/新比率。

    Returns:
        list[dict]: [{"ticker", "effective_date", "action_type", "ratio",
                      "source", "note"}]
    """
    if none_df is None or fwd_df is None or none_df.empty or fwd_df.empty:
        return []
    n = none_df[["date", "close"]].copy()
    f = fwd_df[["date", "close"]].copy()
    for d in (n, f):
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    m = n.merge(f, on="date", suffixes=("_none", "_fwd")).sort_values("date")
    m = m[(m["close_none"] > 0) & (m["close_fwd"] > 0)]
    if len(m) < 2:
        return []
    ratio = m["close_none"] / m["close_fwd"]
    actions = []
    prev_r = float(ratio.iloc[0])
    for i in range(1, len(m)):
        r = float(ratio.iloc[i])
        if prev_r > 0 and abs(r / prev_r - 1) > 0.20:
            # 阶跃确认：新比率需与前一日的差异超过 20%（拆分），
            # ratio = 1 旧股 → ratio 新股
            actions.append({
                "ticker": ticker,
                "effective_date": m.iloc[i]["date"],
                "action_type": "split",
                "ratio": round(prev_r / r, 6),
                "source": "ifind_none_fwd_ratio",
                "note": f"none/fwd 比率 {prev_r:.4f}→{r:.4f}",
            })
        prev_r = r
    return actions


def _splice_ticker_real_prices(db, ticker, db_df, none_df, fwd_df, calendar,
                               end_date, result, info):
    """单只标的的真实价格拼接（真实价格架构 v3，设计文档 2.4 节）。

    与旧 _splice_ticker_with_fwd（等效价）的根本区别：
      - open/high/low/close/volume 存 iFinD **none 真实值**（交易/估值用）；
      - adj_close = fwd × 校准因子（评分用，校准因子 = 库内 adj_close(锚日)/fwd(锚日)，
        保持与库内既有 adj 尺度衔接）；
      - **volume 不乘任何因子**——杜绝凭空制造放量；
      - 拆分自动检测并写入 corporate_actions + result["corporate_actions"]。

    Parameters:
        db: ETFDatabase 实例
        ticker: 标的代码
        db_df: 数据库已有数据（date/close/adj_close，str 日期，已排序）
        none_df: iFinD none 序列（真实价量，标准化列，可为 None）
        fwd_df: iFinD forward 序列（复权连续，标准化列，可为 None）
        calendar: 交易日历（str 日期列表，升序）
        end_date: 'YYYY-MM-DD'
        result: v2 总结果 dict（写入 gaps / splice_factors / saved_count /
                corporate_actions）
        info: per_ticker 条目（写入 new_rows / latest_date / error）
    """
    if none_df is None or none_df.empty:
        info["error"] = "iFinD none 未返回该标的数据"
        return
    if fwd_df is None or fwd_df.empty:
        info["error"] = "iFinD forward 未返回该标的数据"
        return
    if db_df is None or db_df.empty:
        info["error"] = "数据库无历史数据，无法确定锚定日，跳过拼接"
        return

    none = none_df.copy()
    none["date"] = pd.to_datetime(none["date"]).dt.strftime("%Y-%m-%d")
    none = none.drop_duplicates(subset="date").sort_values("date")
    none_map = none.set_index("date")
    none_dates = set(none["date"])
    none_max = none["date"].max()

    fwd = fwd_df.copy()
    fwd["date"] = pd.to_datetime(fwd["date"]).dt.strftime("%Y-%m-%d")
    fwd = fwd.drop_duplicates(subset="date").sort_values("date")
    fwd_map = fwd.set_index("date")
    fwd_dates = set(fwd["date"])

    # 拆分检测（对整个拉取窗口，不限于缺失段）
    for a in _detect_splits_from_none_fwd(ticker, none, fwd):
        result["corporate_actions"].append(a)
        if db is not None:
            try:
                db.save_corporate_actions(pd.DataFrame([a]))
            except Exception:
                pass  # 登记失败不阻断拼接

    first_db = db_df["date"].min()
    last_db = db_df["date"].max()
    existing_dates = set(db_df["date"])

    # 缺失日 = 交易日历 ∩ (首条数据日, end_date] − 已有日期
    # 无论后续是否成功补入，全部计入 gaps 报告
    missing = [d for d in calendar if first_db < d <= end_date and d not in existing_dates]
    info["gaps"] = list(missing)
    for d in missing:
        result["gaps"].append({"ticker": ticker, "date": d})

    if not missing:
        info["latest_date"] = last_db
        result["splice_factors"][ticker] = None  # 无缺失，无需拼接
        return

    # 按连续缺失段切分，每段锚定到段首之前最近的已有交易日
    all_days = sorted(d for d in (existing_dates | set(missing)) if first_db < d <= end_date)
    segments = []  # [(anchor_date, [missing dates])]
    last_existing = first_db
    current = []
    for d in all_days:
        if d in existing_dates:
            if current:
                segments.append((last_existing, current))
                current = []
            last_existing = d
        else:
            current.append(d)
    if current:
        segments.append((last_existing, current))

    db_close_map = db_df.set_index("date")["close"]
    db_adj_map = (db_df.set_index("date")["adj_close"]
                  if "adj_close" in db_df.columns else db_close_map)
    rows = []
    factors = {}
    for anchor, days in segments:
        # 锚定日必须同时存在于 none 与 fwd 序列中（理论上必然存在，属防御性检查）
        if anchor not in none_dates or anchor not in fwd_dates:
            raise ValueError(f"锚定日 {anchor} 不在 iFinD 序列中，无法拼接")
        fwd_close_a = float(fwd_map.loc[anchor, "close"])
        if not np.isfinite(fwd_close_a) or fwd_close_a == 0:
            raise ValueError(f"锚定日 {anchor} 的 iFinD forward 收盘价无效: {fwd_close_a}")
        db_adj_a = float(db_adj_map.loc[anchor])
        if not np.isfinite(db_adj_a) or db_adj_a == 0:
            db_adj_a = float(db_close_map.loc[anchor])
        # 校准因子：库内 adj 尺度 / fwd 尺度（保持评分序列尺度衔接）
        factor = db_adj_a / fwd_close_a
        factors[anchor] = factor

        for d in days:
            # 超出已拉取区间（如 iFinD 数据只到某日）的缺失日保持缺口，不生成数据
            if d not in none_dates or d not in fwd_dates or d > none_max:
                continue
            r = none_map.loc[d]
            rows.append({
                "ticker": ticker,
                "date": d,
                # 真实市场价（交易/估值/止损唯一合法口径）
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                # 真实成交量：iFinD none 原值，绝不乘价格校准因子
                # （真实价格架构 v3：杜绝凭空制造放量；评分量由
                #   apply_adjustment_for_scoring 按 adj_factor 另行换算）
                "volume": float(r["volume"]),
                # 前复权连续价（评分唯一价格来源）
                "adj_close": float(fwd_map.loc[d, "close"]) * factor,
                "source": IFIND_SOURCE_LABEL,
                "adjust_type": "real_spliced",
            })

    if len(factors) == 1:
        result["splice_factors"][ticker] = list(factors.values())[0]
    else:
        result["splice_factors"][ticker] = dict(factors)

    if rows:
        new_df = pd.DataFrame(rows)
        saved = db.save_market_data(new_df) if db is not None else len(new_df)
        info["new_rows"] = int(saved)
        result["saved_count"] += int(saved)

    new_dates = [r["date"] for r in rows]
    info["latest_date"] = max([last_db] + new_dates) if new_dates else last_db


def update_latest_data_v2(db=None, tickers=None, source='ifind', end_date=None,
                          on_progress=None):
    """增量更新最新行情（v2），返回逐只明细与拼接因子的结构化结果。

    设计文档: docs/DATA_UPDATE_FIX_v2_DESIGN.md 第 3.2 节

    Parameters:
        db: ETFDatabase 实例（正式库或临时库）
        tickers: 标的列表，默认正式池（16 行业ETF + 防御 + 000300.SH）
        source: 'ifind'（就近锚定拼接）| 'akshare'（原路径逐只增量，无预检全停）
        end_date: 'YYYY-MM-DD'，默认今天
        on_progress: 可选回调 on_progress(ticker, done, total, message)

    Returns:
        dict: {
            "status": "success" | "partial" | "no_data" | "source_unavailable",
            "saved_count": int,       # 实际写入条数（INSERT OR IGNORE 幂等，不重复计）
            "fetched_count": int,     # 从数据源拉到的总行数
            "per_ticker": {t: {"new_rows", "latest_date", "gaps", "error"}},
            "gaps": [{"ticker", "date"}],   # 全库中间缺口清单（无论是否补入）
            "splice_factors": {t: float 或 {锚定日: factor}},  # 拼接因子披露
            "trading_calendar_end": str | None,
            "elapsed_seconds": float,
            "message": str,
        }

    注意: 本函数不调用 backup_database（备份职责在上层流程）；
          不覆盖/修改任何已有历史行情行。
    """
    t0 = time.time()
    if tickers is None:
        tickers = _official_pool_tickers()
    if end_date is None:
        end_date = datetime.now().strftime('%Y-%m-%d')

    result = {
        "status": "no_data",
        "saved_count": 0,
        "fetched_count": 0,
        "per_ticker": {},
        "gaps": [],
        "splice_factors": {},
        "precheck_breaks": {},
        "corporate_actions": [],
        "trading_calendar_end": None,
        "elapsed_seconds": 0.0,
        "message": "",
    }

    if source == 'akshare':
        return _update_latest_data_v2_akshare(db, tickers, end_date, on_progress, result, t0)

    # ---------------- iFinD 路径 ----------------
    if db is None:
        result["message"] = "db 未提供，无法读取已有行情做锚定拼接"
        result["elapsed_seconds"] = time.time() - t0
        return result

    if ifind_gateway is None:
        result["status"] = "source_unavailable"
        result["message"] = "ifind_gateway 模块导入失败，请检查 src/ifind_gateway.py"
        result["elapsed_seconds"] = time.time() - t0
        return result

    availability = ifind_gateway.is_available()
    if not availability.get("available"):
        result["status"] = "source_unavailable"
        result["message"] = availability.get("message", "iFinD 网关不可用") + \
            "。可改用 source='akshare' 或 iFinD CSV 导入。"
        result["elapsed_seconds"] = time.time() - t0
        return result

    # 库内口径断裂预检：发现疑似断裂时不允许误报"全部成功"
    precheck_breaks = detect_splice_breaks(db, tickers)
    result["precheck_breaks"] = precheck_breaks

    # 读取每只标的的已有数据（date, close, adj_close——adj 用于校准因子保持尺度衔接）
    existing = {}
    last_dates = []
    for t in tickers:
        try:
            df_t = db.get_market_data(ticker=t)
        except Exception:
            df_t = pd.DataFrame()
        if df_t is None or df_t.empty:
            existing[t] = pd.DataFrame(columns=["date", "close", "adj_close"])
        else:
            cols = [c for c in ["date", "close", "adj_close"] if c in df_t.columns]
            d = df_t[cols].copy()
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            existing[t] = d.sort_values("date").reset_index(drop=True)
            last_dates.append(d["date"].max())

    if not last_dates:
        result["status"] = "no_data"
        result["message"] = ("正式池标的在数据库中均无历史数据，无法确定锚定日；"
                             "请先通过全量导入建立基准数据。")
        result["elapsed_seconds"] = time.time() - t0
        return result

    # 统一拉取区间：min(各标的最后已有日) - 10 个自然日，一次拉全区间
    unified_start = (pd.to_datetime(min(last_dates)) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")

    # 真实价格架构 v3：双序列拉取
    #   none（不复权）→ 存真实 OHLCV（交易/估值唯一合法口径）
    #   forward（前复权）→ 生成 adj_close（评分唯一价格来源）+ 拆分检测
    gw_none = ifind_gateway.get_price(list(tickers), unified_start, end_date, adjust="none")
    gw_fwd = ifind_gateway.get_price(list(tickers), unified_start, end_date, adjust="forward")
    raw_none = gw_none.get("df", pd.DataFrame())
    raw_fwd = gw_fwd.get("df", pd.DataFrame())
    gw_failed = dict(gw_none.get("failed", {}) or {})
    gw_failed.update(gw_fwd.get("failed", {}) or {})
    result["fetched_count"] = (0 if raw_none is None else len(raw_none)) + \
                              (0 if raw_fwd is None else len(raw_fwd))

    # 交易日历 = iFinD 000300.SH 的日期序列；基准缺失时退化为全部返回日期的并集
    calendar = []
    cal_src = raw_none if raw_none is not None and not raw_none.empty else raw_fwd
    if cal_src is not None and not cal_src.empty:
        if BENCHMARK in set(cal_src["ticker"]):
            calendar = sorted(cal_src.loc[cal_src["ticker"] == BENCHMARK, "date"].unique().tolist())
        else:
            calendar = sorted(cal_src["date"].unique().tolist())
    result["trading_calendar_end"] = calendar[-1] if calendar else None

    none_by_ticker = {}
    if raw_none is not None and not raw_none.empty:
        none_by_ticker = {t: g for t, g in raw_none.groupby("ticker")}
    fwd_by_ticker = {}
    if raw_fwd is not None and not raw_fwd.empty:
        fwd_by_ticker = {t: g for t, g in raw_fwd.groupby("ticker")}

    # 逐只处理：独立 try/except，单只失败不阻断
    for idx, t in enumerate(tickers):
        if on_progress is not None:
            try:
                on_progress(t, idx + 1, len(tickers), "拼接处理中")
            except Exception:
                pass
        info = {"new_rows": 0, "latest_date": None, "gaps": [], "error": None}
        result["per_ticker"][t] = info
        try:
            if t in gw_failed:
                raise RuntimeError(f"iFinD 网关失败: {gw_failed[t]}")
            _splice_ticker_real_prices(db, t, existing.get(t),
                                       none_by_ticker.get(t), fwd_by_ticker.get(t),
                                       calendar, end_date, result, info)
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"

    errors = [t for t, i in result["per_ticker"].items() if i["error"]]
    result["elapsed_seconds"] = time.time() - t0

    # 预检发现库内历史断裂：即使本次拼接全部成功，也不得宣称"全部成功"，
    # 状态压至 partial 并在 message 中明示，防止在断裂数据上误报
    break_note = ""
    if precheck_breaks:
        break_note = (
            f"；⚠️ 预检发现 {len(precheck_breaks)} 只标的存在疑似历史口径断裂"
            f"（日收益率超阈值，如拆分前后混排），请先修复历史数据再更新："
            + ", ".join(f"{t}({v[0]['date']} {v[0]['return']:+.1%})" for t, v in list(precheck_breaks.items())[:5])
        )

    if result["saved_count"] > 0:
        result["status"] = "partial" if (errors or precheck_breaks) else "success"
        result["message"] = (
            f"iFinD 拼接更新{'部分' if errors else ''}成功：写入 {result['saved_count']} 条，"
            f"成功 {len(tickers) - len(errors)} 只，失败 {len(errors)} 只"
            + (f"（{', '.join(errors[:5])}{' 等' if len(errors) > 5 else ''}）" if errors else "")
            + break_note
        )
    elif errors:
        result["status"] = "no_data"
        result["message"] = (
            f"所有标的均未写入（共 {len(errors)} 只失败："
            f"{', '.join(errors[:5])}{' 等' if len(errors) > 5 else ''}）。"
            "缺口与失败明细见下方逐只结果。"
            + break_note
        )
    else:
        result["status"] = "no_data"
        result["message"] = "所有标的已是最新，无需更新" + break_note

    return result


def _update_latest_data_v2_akshare(db, tickers, end_date, on_progress, result, t0):
    """v2 的 AKShare 路径：逐只增量拉取，无'预检失败全停'逻辑。

    复用 DataFetcher 的单只获取与重试机制；拉取起点为每只标的自己的
    最后已有日（而非全库 MAX），修复原实现漏掉 per-ticker 缺口的问题。
    成交量/复权口径与旧 update_latest_data 保持一致（volume 原样，qfq）。
    """
    # AKShare 路径同样先做库内断裂预检，防止在断裂数据上追加并误报成功
    if db is not None:
        result["precheck_breaks"] = detect_splice_breaks(db, tickers)

    try:
        fetcher = DataFetcher()
    except ImportError as e:
        result["status"] = "source_unavailable"
        result["message"] = (
            f"AKShare 未安装: {e}\n请运行: pip install akshare\n"
            "或改用 source='ifind' / iFinD CSV 导入方式。"
        )
        result["elapsed_seconds"] = time.time() - t0
        return result

    for idx, t in enumerate(tickers):
        if on_progress is not None:
            try:
                on_progress(t, idx + 1, len(tickers), "AKShare 拉取中")
            except Exception:
                pass
        info = {"new_rows": 0, "latest_date": None, "gaps": [], "error": None}
        result["per_ticker"][t] = info
        try:
            code = t.split('.')[0]
            df_t = db.get_market_data(ticker=t) if db is not None else pd.DataFrame()
            if df_t is not None and not df_t.empty:
                last_d = pd.to_datetime(df_t["date"]).max()
                start = (last_d + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                # 无历史数据时给一个保守默认窗口（AKShare 路径无法锚定，直接存原始 qfq）
                start = (pd.to_datetime(end_date) - timedelta(days=30)).strftime("%Y-%m-%d")

            if pd.to_datetime(start) > pd.to_datetime(end_date):
                info["latest_date"] = last_d.strftime("%Y-%m-%d")
                continue

            if t == BENCHMARK:
                new_df = fetcher.fetch_index_history(code, start, end_date)
            else:
                new_df = fetcher.fetch_etf_history(code, start, end_date)

            if new_df is None or new_df.empty:
                if t in fetcher.failed_tickers:
                    raise RuntimeError(f"AKShare 获取失败（含重试）: {t}")
                info["latest_date"] = last_d.strftime("%Y-%m-%d") \
                    if df_t is not None and not df_t.empty else None
                continue

            new_df = new_df[new_df["ticker"] == t].copy()
            if new_df.empty:
                info["latest_date"] = last_d.strftime("%Y-%m-%d") \
                    if df_t is not None and not df_t.empty else None
                continue

            result["fetched_count"] += len(new_df)
            saved = db.save_market_data(new_df) if db is not None else len(new_df)
            info["new_rows"] = int(saved)
            result["saved_count"] += int(saved)
            info["latest_date"] = pd.to_datetime(new_df["date"]).max().strftime("%Y-%m-%d")
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"

    errors = [t for t, i in result["per_ticker"].items() if i["error"]]
    result["elapsed_seconds"] = time.time() - t0

    if result["saved_count"] > 0:
        result["status"] = "partial" if errors else "success"
        result["message"] = (
            f"AKShare 更新{'部分' if errors else ''}成功：写入 {result['saved_count']} 条，"
            f"失败 {len(errors)} 只"
            + (f"（{', '.join(errors[:5])}{' 等' if len(errors) > 5 else ''}）" if errors else "")
        )
    elif errors:
        result["status"] = "no_data"
        result["message"] = f"AKShare 请求失败（{len(errors)} 只标的，含重试后仍失败）"
    else:
        result["status"] = "no_data"
        result["message"] = "所有标的已是最新，无需更新"

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

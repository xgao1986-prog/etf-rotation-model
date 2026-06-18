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
    """本地数据获取器 - 仅AKShare"""
    
    def __init__(self):
        self._ak = None  # 懒加载AKShare
    
    def _get_akshare(self):
        """懒加载AKShare"""
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak
    
    def fetch_etf_history(self, code, start_date, end_date=None, adjust='qfq') -> pd.DataFrame:
        """获取单只ETF历史行情"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        
        try:
            ak = self._get_akshare()
            
            df = ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust=adjust
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
            
        except Exception as e:
            warnings.warn(f"AKShare获取 {code} 失败: {e}")
            return pd.DataFrame()
    
    def fetch_index_history(self, code, start_date, end_date=None) -> pd.DataFrame:
        """获取指数历史行情"""
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        
        try:
            ak = self._get_akshare()
            
            df = ak.index_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', '')
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
            
        except Exception as e:
            warnings.warn(f"AKShare获取指数 {code} 失败: {e}")
            return pd.DataFrame()
    
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
        """获取最新一天的数据（增量更新）"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        if db:
            latest = db.get_latest_date()
            if latest:
                start = (pd.to_datetime(latest) - timedelta(days=5)).strftime('%Y-%m-%d')
            else:
                start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        else:
            start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        
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
    
    fetcher = DataFetcher()
    
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
    """增量更新最新数据（AKShare）
    
    v1.2.2: 更新前自动备份数据库
    """
    # 更新前自动备份
    if db is not None:
        try:
            import backup_database
            backup_database.backup_database()
            print("✅ 数据库已备份")
        except Exception as e:
            print(f"⚠️ 备份失败（继续更新）: {e}")
    
    fetcher = DataFetcher()
    
    print(f"AKShare增量更新...")
    df = fetcher.fetch_latest(db=db)
    
    if df.empty:
        print("无新数据")
        return 0
    
    if db is not None:
        count = db.save_market_data(df)
        print(f"已更新 {count} 条记录")
        return count
    
    return len(df)


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

"""
数据获取模块 - iFinD主 + AKShare备 混合数据源

设计原则:
  1. 优先iFinD（数据质量高，已付费）
  2. iFinD失败/缺失时，自动回退到AKShare
  3. 已下载数据从本地数据库读取，避免重复获取
  4. iFinD限制: 每批最多10个ticker，每段最多3年

使用方式:
  from data_fetcher import HybridDataFetcher, download_all_data, update_latest_data
  fetcher = HybridDataFetcher()
  df = fetcher.fetch_all_etfs('2019-06-03', db=db)  # db为ETFDatabase实例
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import warnings
import os

from config import (
    ETF_UNIVERSE, BENCHMARK, ETF_CODES, BENCHMARK_CODE,
    DATA_SOURCE, DB_PATH
)


class HybridDataFetcher:
    """
    混合数据获取器
    
    获取优先级:
      1. 本地数据库 (已有数据直接返回)
      2. iFinD (主数据源，高质量)
      3. AKShare (备用，免费)
    """
    
    def __init__(self, primary=None, backup=None):
        self.primary = primary or DATA_SOURCE['primary']
        self.backup = backup or DATA_SOURCE['backup']
        self._ak = None        # 懒加载AKShare
        self._ifind = None     # 懒加载iFinD
        
        # iFinD限制
        self.ifind_max_tickers = DATA_SOURCE.get('ifind_max_tickers_per_query', 10)
        self.ifind_max_years = DATA_SOURCE.get('ifind_max_years_per_query', 3)
    
    # ==================== 懒加载数据源 ====================
    
    def _get_akshare(self):
        """懒加载AKShare"""
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak
    
    def _get_ifind(self):
        """懒加载iFinD"""
        if self._ifind is None:
            try:
                # iFinD Python API (同花顺iFinD)
                # 注意: 需要安装 ifind 包并配置账号
                import iFinDPy
                user = DATA_SOURCE.get('ifind_username', '')
                pwd = DATA_SOURCE.get('ifind_password', '')
                
                if not user or not pwd:
                    warnings.warn("iFinD账号未配置，将使用AKShare")
                    return None
                
                # 登录iFinD
                iFinDPy.THS_iFinDLogin(user, pwd)
                self._ifind = iFinDPy
                print("iFinD登录成功")
                
            except ImportError:
                warnings.warn("iFinD Python包未安装，将使用AKShare")
                return None
            except Exception as e:
                warnings.warn(f"iFinD登录失败: {e}，将使用AKShare")
                return None
        
        return self._ifind
    
    # ==================== iFinD数据获取 ====================
    
    def _fetch_ifind_price(self, tickers, start_date, end_date) -> pd.DataFrame:
        """
        通过iFinD获取价格数据
        
        Parameters:
            tickers: list of ticker strings (如 ['512480.SH', '515880.SH'])
            start_date: 'YYYY-MM-DD'
            end_date: 'YYYY-MM-DD'
        
        Returns:
            DataFrame with columns: [ticker, date, open, high, low, close, volume, amount]
        """
        ifind = self._get_ifind()
        if ifind is None:
            return pd.DataFrame()  # iFinD不可用，返回空
        
        try:
            # iFinD接口: THS_HQ 获取行情数据
            # 参数: 标的代码, 指标, 开始日期, 结束日期
            ticker_str = ','.join(tickers)
            indicators = 'open;high;low;close;volume;amount'  # 开盘价;最高价;最低价;收盘价;成交量;成交额
            
            # 调用iFinD接口
            result = ifind.THS_HQ(ticker_str, indicators, start_date.replace('-', ''), end_date.replace('-', ''))
            
            if result is None or result.empty:
                return pd.DataFrame()
            
            # iFinD返回格式转换
            # 注意: 实际格式可能不同，需要根据实际返回调整
            df = result.copy()
            
            # 标准化列名
            column_mapping = {
                'thscode': 'ticker',
                'time': 'date',
                'OPEN': 'open',
                'HIGH': 'high',
                'LOW': 'low',
                'CLOSE': 'close',
                'VOLUME': 'volume',
                'AMOUNT': 'amount',
            }
            
            df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})
            
            # 确保必要列存在
            required_cols = ['ticker', 'date', 'close']
            for col in required_cols:
                if col not in df.columns:
                    warnings.warn(f"iFinD返回数据缺少列: {col}")
                    return pd.DataFrame()
            
            # 日期格式转换
            df['date'] = pd.to_datetime(df['date'])
            
            # 数值转换
            numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # 添加adj_close（iFinD通常已复权）
            df['adj_close'] = df['close']
            
            # 选择标准列
            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close']
            df = df[[c for c in cols if c in df.columns]]
            
            return df.sort_values(['ticker', 'date']).reset_index(drop=True)
            
        except Exception as e:
            warnings.warn(f"iFinD获取数据失败: {e}")
            return pd.DataFrame()
    
    def _fetch_ifind_in_batches(self, tickers, start_date, end_date) -> pd.DataFrame:
        """
        分批获取iFinD数据（处理查询限制）
        
        iFinD限制:
          - 每批最多10个ticker
          - 每段最多3年
        """
        all_data = []
        
        # 1. 按ticker分批
        ticker_batches = [
            tickers[i:i + self.ifind_max_tickers]
            for i in range(0, len(tickers), self.ifind_max_tickers)
        ]
        
        # 2. 按日期分段（每段最多3年）
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        
        date_ranges = []
        current_start = start_dt
        
        while current_start < end_dt:
            current_end = min(current_start + timedelta(days=365 * self.ifind_max_years - 1), end_dt)
            date_ranges.append((
                current_start.strftime('%Y-%m-%d'),
                current_end.strftime('%Y-%m-%d')
            ))
            current_start = current_end + timedelta(days=1)
        
        # 3. 分批获取
        for batch_idx, ticker_batch in enumerate(ticker_batches):
            for date_start, date_end in date_ranges:
                print(f"  iFinD获取: 批次{batch_idx+1}/{len(ticker_batches)}, 日期{date_start}~{date_end}")
                
                df = self._fetch_ifind_price(ticker_batch, date_start, date_end)
                
                if not df.empty:
                    all_data.append(df)
                
                time.sleep(0.5)  # 礼貌请求，避免频率限制
        
        if not all_data:
            return pd.DataFrame()
        
        return pd.concat(all_data, ignore_index=True)
    
    # ==================== AKShare数据获取（备用） ====================
    
    def _fetch_akshare_etf(self, code, start_date, end_date=None, adjust='qfq') -> pd.DataFrame:
        """通过AKShare获取单只ETF数据"""
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
            
            # 标准化列名
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
            df['ticker'] = f"{code}.SH" if code.startswith('5') or code.startswith('1') else f"{code}.SZ"
            df['adj_close'] = df['close']
            
            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close']
            df = df[[c for c in cols if c in df.columns]]
            
            return df.sort_values('date').reset_index(drop=True)
            
        except Exception as e:
            warnings.warn(f"AKShare获取 {code} 失败: {e}")
            return pd.DataFrame()
    
    def _fetch_akshare_index(self, code, start_date, end_date=None) -> pd.DataFrame:
        """通过AKShare获取指数数据"""
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
            
            cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adj_close']
            df = df[[c for c in cols if c in df.columns]]
            
            return df.sort_values('date').reset_index(drop=True)
            
        except Exception as e:
            warnings.warn(f"AKShare获取指数 {code} 失败: {e}")
            return pd.DataFrame()
    
    # ==================== 核心获取逻辑 ====================
    
    def fetch_all_etfs(self, start_date, end_date=None, db=None, delay=0.5) -> pd.DataFrame:
        """
        获取所有ETF和基准数据（智能混合策略）
        
        Parameters:
            start_date: 开始日期 'YYYY-MM-DD'
            end_date: 结束日期，默认今天
            db: ETFDatabase实例，用于检查已有数据
            delay: 请求间隔
        
        Returns:
            DataFrame with all market data
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        
        all_tickers = list(ETF_UNIVERSE.keys()) + [BENCHMARK]
        
        # 1. 检查数据库已有数据
        existing_data = pd.DataFrame()
        missing_ranges = {}  # {ticker: (needed_start, needed_end)}
        
        if db is not None:
            print("检查数据库已有数据...")
            
            for ticker in all_tickers:
                min_date, max_date = db.get_date_range(ticker)
                
                if min_date is None:
                    # 完全缺失
                    missing_ranges[ticker] = (start_date, end_date)
                else:
                    # 检查是否需要补充
                    needed_start = pd.to_datetime(start_date)
                    needed_end = pd.to_datetime(end_date)
                    existing_start = pd.to_datetime(min_date)
                    existing_end = pd.to_datetime(max_date)
                    
                    # 获取已有数据
                    ticker_df = db.get_market_data(ticker=ticker, start_date=start_date, end_date=end_date)
                    if not ticker_df.empty:
                        existing_data = pd.concat([existing_data, ticker_df], ignore_index=True)
                    
                    # 检查缺失区间
                    gaps = []
                    if needed_start < existing_start:
                        gaps.append((needed_start.strftime('%Y-%m-%d'), 
                                   (existing_start - timedelta(days=1)).strftime('%Y-%m-%d')))
                    if needed_end > existing_end:
                        gaps.append((existing_end.strftime('%Y-%m-%d'), 
                                   needed_end.strftime('%Y-%m-%d')))
                    
                    if gaps:
                        # 合并连续区间
                        merged_start = min(g[0] for g in gaps)
                        merged_end = max(g[1] for g in gaps)
                        missing_ranges[ticker] = (merged_start, merged_end)
            
            print(f"数据库已有: {len(existing_data)} 条记录")
            print(f"需要补充: {len(missing_ranges)} 只标的")
        else:
            # 无数据库，全部获取
            for ticker in all_tickers:
                missing_ranges[ticker] = (start_date, end_date)
        
        if not missing_ranges:
            print("所有数据已在数据库中，无需下载")
            return existing_data
        
        # 2. 尝试iFinD获取缺失数据
        new_data = pd.DataFrame()
        
        if self.primary == 'ifind':
            print(f"\n使用iFinD获取缺失数据...")
            
            missing_tickers = list(missing_ranges.keys())
            
            # 统一日期范围（取所有缺失区间的并集）
            all_starts = [pd.to_datetime(r[0]) for r in missing_ranges.values()]
            all_ends = [pd.to_datetime(r[1]) for r in missing_ranges.values()]
            unified_start = min(all_starts).strftime('%Y-%m-%d')
            unified_end = max(all_ends).strftime('%Y-%m-%d')
            
            ifind_df = self._fetch_ifind_in_batches(missing_tickers, unified_start, unified_end)
            
            if not ifind_df.empty:
                print(f"iFinD获取成功: {len(ifind_df)} 条")
                new_data = pd.concat([new_data, ifind_df], ignore_index=True)
                
                # 标记iFinD已获取的标的
                ifind_tickers = set(ifind_df['ticker'].unique())
                for ticker in list(missing_ranges.keys()):
                    if ticker in ifind_tickers:
                        # 检查数据是否完整
                        ticker_ifind = ifind_df[ifind_df['ticker'] == ticker]
                        needed_start, needed_end = missing_ranges[ticker]
                        
                        if (ticker_ifind['date'].min().strftime('%Y-%m-%d') <= needed_start and
                            ticker_ifind['date'].max().strftime('%Y-%m-%d') >= needed_end):
                            del missing_ranges[ticker]
        
        # 3. iFinD未获取到的，用AKShare补充
        if missing_ranges and self.backup == 'akshare':
            print(f"\n使用AKShare补充 {len(missing_ranges)} 只标的...")
            
            for ticker, (t_start, t_end) in missing_ranges.items():
                code = ticker.split('.')[0]
                
                if ticker == BENCHMARK:
                    # 基准用指数接口
                    df = self._fetch_akshare_index(BENCHMARK_CODE, t_start, t_end)
                else:
                    # ETF用基金接口
                    df = self._fetch_akshare_etf(code, t_start, t_end)
                
                if not df.empty:
                    new_data = pd.concat([new_data, df], ignore_index=True)
                    print(f"  AKShare获取 {ticker}: {len(df)} 条")
                else:
                    print(f"  ⚠️ AKShare获取 {ticker} 失败")
                
                time.sleep(delay)
        
        # 4. 合并并保存
        if not new_data.empty and db is not None:
            count = db.save_market_data(new_data)
            print(f"\n已保存 {count} 条新记录到数据库")
        
        # 5. 返回全部数据（已有 + 新下载）
        if not existing_data.empty and not new_data.empty:
            return pd.concat([existing_data, new_data], ignore_index=True)
        elif not existing_data.empty:
            return existing_data
        elif not new_data.empty:
            return new_data
        else:
            return pd.DataFrame()
    
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
        
        return self.fetch_all_etfs(start, today, db=db, delay=0.3)
    
    def get_data_summary(self, df: pd.DataFrame) -> dict:
        """获取数据摘要"""
        if df.empty:
            return {'status': 'empty'}
        
        return {
            'status': 'ok',
            'total_rows': len(df),
            'tickers': df['ticker'].nunique(),
            'date_range': (df['date'].min().strftime('%Y-%m-%d'),
                          df['date'].max().strftime('%Y-%m-%d')),
            'trading_days': df['date'].nunique(),
        }


# ==================== 便捷函数 ====================

def download_all_data(start_date='2019-06-03', end_date=None, db=None):
    """
    下载所有数据并保存到数据库
    
    Usage:
        from data_fetcher import download_all_data
        from database import ETFDatabase
        db = ETFDatabase()
        download_all_data('2019-06-03', db=db)
    """
    fetcher = HybridDataFetcher()
    
    print(f"="*60)
    print(f"开始下载数据: {start_date} ~ {end_date or '今天'}")
    print(f"主数据源: {fetcher.primary}, 备用: {fetcher.backup}")
    print(f"="*60)
    
    df = fetcher.fetch_all_etfs(start_date, end_date, db=db)
    
    if df.empty:
        print("\n⚠️ 下载失败，无数据")
        return pd.DataFrame()
    
    summary = fetcher.get_data_summary(df)
    print(f"\n{'='*60}")
    print(f"下载完成:")
    print(f"  总记录数: {summary['total_rows']:,}")
    print(f"  标的数量: {summary['tickers']}")
    print(f"  日期范围: {summary['date_range'][0]} ~ {summary['date_range'][1]}")
    print(f"  交易日数: {summary['trading_days']}")
    print(f"{'='*60}")
    
    return df


def update_latest_data(db=None):
    """更新最新数据（增量）"""
    fetcher = HybridDataFetcher()
    
    print(f"增量更新...")
    df = fetcher.fetch_latest(db=db)
    
    if df.empty:
        print("无新数据")
        return 0
    
    summary = fetcher.get_data_summary(df)
    print(f"更新完成: {summary['total_rows']} 条记录")
    
    return summary['total_rows']


if __name__ == '__main__':
    # 测试数据获取
    print("测试混合数据获取器...")
    fetcher = HybridDataFetcher()
    
    # 测试单只ETF（通过AKShare，因为iFinD需要账号）
    df = fetcher._fetch_akshare_etf('512480', '2024-01-01')
    print(f"\n512480数据: {len(df)} 条")
    if not df.empty:
        print(df.head(3))
    
    # 测试指数
    bench = fetcher._fetch_akshare_index('000300', '2024-01-01')
    print(f"\n沪深300数据: {len(bench)} 条")
    if not bench.empty:
        print(bench.head(3))

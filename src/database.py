"""
数据库模块 - SQLite封装
负责所有数据库操作：建表、插入、查询、更新
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from contextlib import contextmanager
import os

from config import DB_PATH


class ETFDatabase:
    """ETF轮动策略数据库管理类"""
    
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._init_tables()
    
    @contextmanager
    def _connect(self):
        """上下文管理器，自动关闭连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def _init_tables(self):
        """初始化所有数据表"""
        with self._connect() as conn:
            cursor = conn.cursor()
            
            # 1. 历史行情数据
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS market_data (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    adj_close REAL,
                    PRIMARY KEY (ticker, date)
                )
            ''')
            
            # 2. 技术指标与评分
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_scores (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    ma20 REAL,
                    ma50 REAL,
                    ma20_slope REAL,
                    above_ma20_days INTEGER,
                    volatility_20 REAL,
                    momentum_20 REAL,
                    volume_ratio REAL,
                    trend_score INTEGER,
                    confirm_score INTEGER,
                    momentum_rank REAL,
                    volume_score INTEGER,
                    vol_score INTEGER,
                    total_score REAL,
                    PRIMARY KEY (ticker, date)
                )
            ''')
            
            # 3. 交易信号记录
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trade_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    name TEXT,
                    signal_type TEXT,  -- 'BUY', 'HOLD', 'SELL', 'STOP_LOSS'
                    close_price REAL,
                    ma20 REAL,
                    total_score REAL,
                    target_weight REAL,
                    actual_weight REAL,
                    reason TEXT,
                    executed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 4. 回测结果存档
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    total_return REAL,
                    annual_return REAL,
                    sharpe_ratio REAL,
                    max_drawdown REAL,
                    num_trades INTEGER,
                    win_rate REAL,
                    params_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 5. 持仓记录（模拟盘/实盘）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS portfolio (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    ticker TEXT,
                    shares INTEGER,
                    cost_basis REAL,
                    high_water REAL,        -- 移动止损最高价
                    stop_loss_date TEXT,    -- 止损日期（冷却期用）
                    current_price REAL,
                    unrealized_pnl REAL,
                    realized_pnl REAL,
                    status TEXT,  -- 'OPEN', 'CLOSED'
                    closed_date TEXT
                )
            ''')
            
            # 6. 运行日志
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    log_type TEXT,
                    message TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 7. 板块指数历史行情（v1.1新增）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sector_market_data (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    amount REAL,
                    adj_close REAL,
                    source TEXT DEFAULT 'AKShare-Sector',
                    PRIMARY KEY (ticker, date)
                )
            ''')
            
            # 8. 板块指数评分（v1.1新增）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sector_scores (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    ma20 REAL,
                    ma50 REAL,
                    ma20_slope REAL,
                    above_ma20_days INTEGER,
                    volatility_20 REAL,
                    momentum_20 REAL,
                    volume_ratio REAL,
                    trend_score INTEGER,
                    confirm_score INTEGER,
                    momentum_rank REAL,
                    volume_score INTEGER,
                    vol_score INTEGER,
                    total_score REAL,
                    sector_boost_value REAL DEFAULT 0,  -- 板块动量加分值
                    PRIMARY KEY (ticker, date)
                )
            ''')
            
            conn.commit()
    
    # ==================== 行情数据操作 ====================
    
    def save_market_data(self, df: pd.DataFrame):
        """保存行情数据，自动去重"""
        if df.empty:
            return 0
        
        # 标准化列名
        required_cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")
        
        # 确保数据类型正确
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        with self._connect() as conn:
            # 使用INSERT OR REPLACE避免重复
            df.to_sql('market_data', conn, if_exists='append', index=False)
            
            # 清理重复数据（保留最新）
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM market_data 
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) 
                    FROM market_data 
                    GROUP BY ticker, date
                )
            ''')
            conn.commit()
            
        return len(df)
    
    def get_market_data(self, ticker=None, start_date=None, end_date=None) -> pd.DataFrame:
        """获取行情数据"""
        query = "SELECT * FROM market_data WHERE 1=1"
        params = []
        
        if ticker:
            if isinstance(ticker, list):
                placeholders = ','.join(['?' for _ in ticker])
                query += f" AND ticker IN ({placeholders})"
                params.extend(ticker)
            else:
                query += " AND ticker = ?"
                params.append(ticker)
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY ticker, date"
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
        
        return df
    
    def get_date_range(self, ticker):
        """获取某标的的数据日期范围"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MIN(date), MAX(date) FROM market_data WHERE ticker = ?",
                (ticker,)
            )
            result = cursor.fetchone()
        
        return result[0], result[1] if result else (None, None)
    
    def get_all_tickers(self):
        """获取数据库中所有标的"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT ticker FROM market_data ORDER BY ticker")
            return [row[0] for row in cursor.fetchall()]
    
    def get_latest_date(self):
        """获取数据库中最新日期"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM market_data")
            result = cursor.fetchone()
        return result[0] if result else None
    
    # ==================== 板块指数数据操作（v1.1新增）====================
    
    def save_sector_data(self, df: pd.DataFrame):
        """保存板块指数行情数据，自动去重"""
        if df.empty:
            return 0
        
        required_cols = ['ticker', 'date', 'open', 'high', 'low', 'close', 'volume']
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")
        
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        with self._connect() as conn:
            df.to_sql('sector_market_data', conn, if_exists='append', index=False)
            
            # 清理重复数据
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM sector_market_data 
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) 
                    FROM sector_market_data 
                    GROUP BY ticker, date
                )
            ''')
            conn.commit()
        
        return len(df)
    
    def get_sector_data(self, ticker=None, start_date=None, end_date=None) -> pd.DataFrame:
        """获取板块指数行情数据"""
        query = "SELECT * FROM sector_market_data WHERE 1=1"
        params = []
        
        if ticker:
            if isinstance(ticker, list):
                placeholders = ','.join(['?' for _ in ticker])
                query += f" AND ticker IN ({placeholders})"
                params.extend(ticker)
            else:
                query += " AND ticker = ?"
                params.append(ticker)
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY ticker, date"
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
        
        return df
    
    def get_all_sector_tickers(self):
        """获取数据库中所有板块指数标的"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT ticker FROM sector_market_data ORDER BY ticker")
            return [row[0] for row in cursor.fetchall()]
    
    def get_sector_latest_date(self):
        """获取板块指数最新日期"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM sector_market_data")
            result = cursor.fetchone()
        return result[0] if result else None
    
    def save_sector_scores(self, df: pd.DataFrame):
        """保存板块指数评分数据"""
        if df.empty:
            return 0
        
        df = df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        with self._connect() as conn:
            df.to_sql('sector_scores', conn, if_exists='append', index=False)
            
            # 清理重复
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM sector_scores 
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) 
                    FROM sector_scores 
                    GROUP BY ticker, date
                )
            ''')
            conn.commit()
        
        return len(df)
    
    def get_sector_scores(self, date=None, ticker=None) -> pd.DataFrame:
        """获取板块指数评分数据"""
        query = "SELECT * FROM sector_scores WHERE 1=1"
        params = []
        
        if date:
            query += " AND date = ?"
            params.append(date)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        
        query += " ORDER BY date, total_score DESC"
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
        
        return df
    
    # ==================== 评分数据操作 ====================
    
    def save_scores(self, df: pd.DataFrame):
        """保存评分数据"""
        if df.empty:
            return 0
        
        df = df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        with self._connect() as conn:
            df.to_sql('daily_scores', conn, if_exists='append', index=False)
            
            # 清理重复
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM daily_scores 
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) 
                    FROM daily_scores 
                    GROUP BY ticker, date
                )
            ''')
            conn.commit()
        
        return len(df)
    
    def get_scores(self, date=None, ticker=None) -> pd.DataFrame:
        """获取评分数据"""
        query = "SELECT * FROM daily_scores WHERE 1=1"
        params = []
        
        if date:
            query += " AND date = ?"
            params.append(date)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        
        query += " ORDER BY date, total_score DESC"
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
        
        return df
    
    # ==================== 信号记录操作 ====================
    
    def save_signals(self, df: pd.DataFrame):
        """保存交易信号"""
        if df.empty:
            return 0
        
        df = df.copy()
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        with self._connect() as conn:
            df.to_sql('trade_signals', conn, if_exists='append', index=False)
            conn.commit()
        
        return len(df)
    
    def get_signals(self, start_date=None, end_date=None, signal_type=None) -> pd.DataFrame:
        """获取交易信号"""
        query = "SELECT * FROM trade_signals WHERE 1=1"
        params = []
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type)
        
        query += " ORDER BY date DESC, total_score DESC"
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
            df['created_at'] = pd.to_datetime(df['created_at'])
        
        return df
    
    # ==================== 回测结果操作 ====================
    
    def save_backtest_result(self, result: dict):
        """保存回测结果"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO backtest_results 
                (run_date, start_date, end_date, total_return, annual_return, 
                 sharpe_ratio, max_drawdown, num_trades, win_rate, params_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                result.get('run_date', datetime.now().strftime('%Y-%m-%d')),
                result.get('start_date'),
                result.get('end_date'),
                result.get('total_return'),
                result.get('annual_return'),
                result.get('sharpe_ratio'),
                result.get('max_drawdown'),
                result.get('num_trades'),
                result.get('win_rate'),
                result.get('params_json', '{}')
            ))
            conn.commit()
    
    def get_backtest_results(self, limit=10) -> pd.DataFrame:
        """获取历史回测结果"""
        with self._connect() as conn:
            df = pd.read_sql_query('''
                SELECT * FROM backtest_results 
                ORDER BY run_date DESC, id DESC 
                LIMIT ?
            ''', conn, params=(limit,))
        
        if not df.empty:
            df['run_date'] = pd.to_datetime(df['run_date'])
            df['created_at'] = pd.to_datetime(df['created_at'])
        
        return df
    
    # ==================== 日志操作 ====================
    
    def log(self, log_type, message, details=None):
        """记录运行日志"""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO run_logs (run_date, log_type, message, details)
                VALUES (?, ?, ?, ?)
            ''', (
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                log_type,
                message,
                details
            ))
            conn.commit()
    
    def get_logs(self, log_type=None, limit=100) -> pd.DataFrame:
        """获取日志"""
        query = "SELECT * FROM run_logs WHERE 1=1"
        params = []
        
        if log_type:
            query += " AND log_type = ?"
            params.append(log_type)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        
        if not df.empty:
            df['created_at'] = pd.to_datetime(df['created_at'])
        
        return df
    
    # ==================== 统计信息 ====================
    
    def get_stats(self) -> dict:
        """获取数据库统计信息"""
        with self._connect() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # 行情数据条数
            cursor.execute("SELECT COUNT(*) FROM market_data")
            stats['market_data_count'] = cursor.fetchone()[0]
            
            # 评分数据条数
            cursor.execute("SELECT COUNT(*) FROM daily_scores")
            stats['scores_count'] = cursor.fetchone()[0]
            
            # 信号数量
            cursor.execute("SELECT COUNT(*) FROM trade_signals")
            stats['signals_count'] = cursor.fetchone()[0]
            
            # 回测记录数
            cursor.execute("SELECT COUNT(*) FROM backtest_results")
            stats['backtest_count'] = cursor.fetchone()[0]
            
            # 最新日期
            cursor.execute("SELECT MAX(date) FROM market_data")
            stats['latest_date'] = cursor.fetchone()[0]
            
            # 最早日期
            cursor.execute("SELECT MIN(date) FROM market_data")
            stats['earliest_date'] = cursor.fetchone()[0]
            
            # 标的数量
            cursor.execute("SELECT COUNT(DISTINCT ticker) FROM market_data")
            stats['ticker_count'] = cursor.fetchone()[0]
            
            return stats


# 单例模式，方便全局使用
db = ETFDatabase()


if __name__ == '__main__':
    # 测试数据库
    database = ETFDatabase()
    stats = database.get_stats()
    print("数据库统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

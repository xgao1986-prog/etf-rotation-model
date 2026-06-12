"""
策略引擎 v1.0 - ETF轮动评分体系
原始参数（表现最优）：
  趋势30% + 确认20% + 动量25% + 成交量15% + 波动率10%

修复内容：
  - 所有指标使用shift(1)，避免未来数据泄露
  - 横截面动量排名在合并全universe后计算
  - generate_signals使用groupby shift防止跨ETF污染
  - 交易费率0.03%双向
  - 仓位控制严格≤5只
  - 大盘择时控制总仓位
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json

from config import STRATEGY_CONFIG, ETF_UNIVERSE, BENCHMARK
from config import ETF_TO_SECTOR_MAPPING, SECTOR_INDEX_UNIVERSE


class StrategyEngine:
    """ETF轮动策略引擎"""
    
    def __init__(self, cfg=None):
        self.cfg = cfg or STRATEGY_CONFIG
        self.tickers = list(ETF_UNIVERSE.keys())
        self.benchmark = BENCHMARK
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算技术指标（使用shift(1)避免未来数据）
        
        Returns:
            DataFrame with technical indicators
        """
        df = df.copy().sort_values('date')
        
        # 均线（用前一日收盘计算，当日决策时用昨日均线）
        df['ma20'] = df['close'].rolling(self.cfg['ma_short']).mean().shift(1)
        df['ma50'] = df['close'].rolling(self.cfg['ma_long']).mean().shift(1)
        
        # 均线斜率（用前一日数据）
        df['ma20_slope'] = df['ma20'].diff().shift(1)
        
        # 20日波动率（用前一日数据）
        df['volatility_20'] = df['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)
        
        # 20日动量（用前一日收盘）
        df['momentum_20'] = df['close'].pct_change(20).shift(1)
        
        # 成交量比率（用前一日）
        df['volume_ma20'] = df['volume'].rolling(20).mean().shift(1)
        df['volume_ratio'] = (df['volume'].shift(1) / df['volume_ma20']).replace([np.inf, -np.inf], 1)
        
        # 是否在均线之上（用前一日收盘 vs 昨日均线）
        df['above_ma20'] = (df['close'].shift(1) > df['ma20']).astype(int)
        
        # 连续在均线之上的天数（用前一日数据累积）
        df['above_ma20_days'] = df['above_ma20'].groupby(
            (df['above_ma20'] == 0).cumsum()
        ).cumsum().shift(1).fillna(0).astype(int)
        
        return df
    
    def calculate_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算ETF评分（不含动量排名，动量排名需全universe合并后计算）
        
        维度：
          趋势强度 30%: 收盘价>20日均线(+15) + >50日均线(+10) + 均线斜率>0(+5)
          趋势确认 20%: 连续在20日均线之上的天数×4分，最多5天(20分)
          动量    25%: 20日收益率的横截面排名（百分位×25）——在rank_all_momentum中计算
          成交量  15%: 放量上涨(+15) / 放量(+10) / 普通(+5)
          波动率  10%: 适中波动率1-4%(+10) / 较高4-6%(+5)
        """
        df = self.calculate_indicators(df)
        
        # 1. 趋势强度 (30分)
        df['trend_score'] = 0
        df.loc[df['close'].shift(1) > df['ma20'], 'trend_score'] += 15
        df.loc[df['close'].shift(1) > df['ma50'], 'trend_score'] += 10
        df.loc[df['ma20_slope'] > 0, 'trend_score'] += 5
        
        # 2. 趋势确认 (20分) - 连续在均线之上的天数
        df['confirm_score'] = np.minimum(df['above_ma20_days'] * 4, 20)
        
        # 3. 动量 (25分) - 横截面排名，需要全universe合并后计算
        # 先标记有效动量值，排名在rank_all_momentum中完成
        df['momentum_valid'] = df['momentum_20'].notna()
        df['momentum_rank'] = np.nan  # 占位，后续填充
        
        # 4. 成交量 (15分)
        df['volume_score'] = 5  # 默认5分
        # 放量: 成交量 > 1.5倍20日均量
        df.loc[df['volume_ratio'] > 1.5, 'volume_score'] = 10
        # 放量上涨: 放量且前一日上涨
        df.loc[(df['volume_ratio'] > 1.5) & (df['close'].shift(1) > df['close'].shift(2)), 'volume_score'] = 15
        
        # 5. 波动率 (10分)
        df['vol_score'] = 0
        vol = df['volatility_20'].abs()
        df.loc[(vol >= 0.01) & (vol <= 0.04), 'vol_score'] = 10
        df.loc[(vol > 0.04) & (vol <= 0.06), 'vol_score'] = 5
        
        return df
    
    def calculate_indicators_and_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        逐ETF计算指标和评分（不含横截面动量排名）
        
        这是main.py signal命令使用的接口。
        横截面动量排名在合并所有ETF后由rank_all_momentum完成。
        """
        return self.calculate_scores(df)
    
    def rank_all_momentum(self, scores_df: pd.DataFrame) -> pd.DataFrame:
        """
        全universe横截面动量排名（必须在合并所有ETF后调用）
        
        按日期分组，对所有ETF的20日动量进行排名，百分位×25
        
        Parameters:
            scores_df: 合并后的所有ETF评分DataFrame（含ticker, date, momentum_20）
        
        Returns:
            DataFrame with momentum_rank filled
        """
        df = scores_df.copy()
        
        # 按日期分组计算动量排名
        dates = df['date'].unique()
        result_dfs = []
        
        for date in dates:
            day_df = df[df['date'] == date].copy()
            valid = day_df[day_df['momentum_valid'] == True]
            
            if len(valid) > 1:
                day_df.loc[valid.index, 'momentum_rank'] = valid['momentum_20'].rank(pct=True) * 25
            elif len(valid) == 1:
                day_df.loc[valid.index, 'momentum_rank'] = 12.5
            
            result_dfs.append(day_df)
        
        df = pd.concat(result_dfs, ignore_index=True)
        df['momentum_rank'] = df['momentum_rank'].fillna(0)
        
        return df
    
    def compute_total_score(self, scores_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算总评分（在横截面动量排名之后调用）
        
        total_score = trend_score + confirm_score + momentum_rank + volume_score + vol_score
        """
        df = scores_df.copy()
        
        df['total_score'] = (
            df['trend_score'].fillna(0) +
            df['confirm_score'].fillna(0) +
            df['momentum_rank'].fillna(0) +
            df['volume_score'].fillna(0) +
            df['vol_score'].fillna(0)
        )
        
        return df
    
    def calculate_total_score(self, df: pd.DataFrame, sector_scores_df=None) -> pd.DataFrame:
        """
        计算总评分（单ETF调用，不含横截面动量排名）
        
        注意：此方法不计算横截面动量排名，因为单只ETF无法做横截面比较。
        横截面排名需调用rank_all_momentum（全universe合并后）。
        
        这是backtest.py run()中逐ETF调用的接口。
        """
        return self.calculate_scores(df)
    
    # ==================== 板块动量增强（v1.1 实验性）====================
    
    def calculate_sector_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算板块指数技术指标（与ETF指标计算逻辑一致）"""
        df = df.copy().sort_values('date')
        
        df['ma20'] = df['close'].rolling(self.cfg['ma_short']).mean().shift(1)
        df['ma50'] = df['close'].rolling(self.cfg['ma_long']).mean().shift(1)
        df['ma20_slope'] = df['ma20'].diff().shift(1)
        df['volatility_20'] = df['close'].pct_change().rolling(20).std().shift(1) * np.sqrt(252)
        df['momentum_20'] = df['close'].pct_change(20).shift(1)
        df['volume_ma20'] = df['volume'].rolling(20).mean().shift(1)
        df['volume_ratio'] = (df['volume'].shift(1) / df['volume_ma20']).replace([np.inf, -np.inf], 1)
        df['above_ma20'] = (df['close'].shift(1) > df['ma20']).astype(int)
        df['above_ma20_days'] = df['above_ma20'].groupby(
            (df['above_ma20'] == 0).cumsum()
        ).cumsum().shift(1).fillna(0).astype(int)
        
        return df
    
    def calculate_sector_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算板块指数评分（与ETF评分体系一致，满分100）"""
        df = self.calculate_sector_indicators(df)
        
        df['trend_score'] = 0
        df.loc[df['close'].shift(1) > df['ma20'], 'trend_score'] += 15
        df.loc[df['close'].shift(1) > df['ma50'], 'trend_score'] += 10
        df.loc[df['ma20_slope'] > 0, 'trend_score'] += 5
        
        df['confirm_score'] = np.minimum(df['above_ma20_days'] * 4, 20)
        df['momentum_valid'] = df['momentum_20'].notna()
        
        df['volume_score'] = 5
        df.loc[df['volume_ratio'] > 1.5, 'volume_score'] = 10
        df.loc[(df['volume_ratio'] > 1.5) & (df['close'].shift(1) > df['close'].shift(2)), 'volume_score'] = 15
        
        df['vol_score'] = 0
        vol = df['volatility_20'].abs()
        df.loc[(vol >= 0.01) & (vol <= 0.04), 'vol_score'] = 10
        df.loc[(vol > 0.04) & (vol <= 0.06), 'vol_score'] = 5
        
        return df
    
    def rank_sector_momentum(self, daily_df: pd.DataFrame) -> pd.DataFrame:
        """计算板块动量横截面排名（百分位×25）"""
        df = daily_df.copy()
        valid = df[df['momentum_valid'] == True].copy()
        
        if len(valid) > 1:
            valid['momentum_rank'] = valid['momentum_20'].rank(pct=True) * 25
            df = df.merge(valid[['ticker', 'date', 'momentum_rank']], 
                         on=['ticker', 'date'], how='left')
        else:
            df['momentum_rank'] = 12.5
        
        df['momentum_rank'] = df['momentum_rank'].fillna(0)
        return df
    
    def calculate_sector_total_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算板块指数总评分"""
        df = self.calculate_sector_scores(df)
        
        dates = df['date'].unique()
        result_dfs = []
        
        for date in dates:
            day_df = df[df['date'] == date].copy()
            day_df = self.rank_sector_momentum(day_df)
            result_dfs.append(day_df)
        
        df = pd.concat(result_dfs, ignore_index=True)
        
        df['total_score'] = (
            df['trend_score'] +
            df['confirm_score'] +
            df['momentum_rank'] +
            df['volume_score'] +
            df['vol_score']
        )
        
        return df
    
    def calculate_sector_boost(self, etf_scores_df: pd.DataFrame, 
                               sector_scores_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算板块动量增强加分
        
        逻辑：
          1. 获取每只ETF对应的板块指数评分
          2. 板块总评分排名前N → +sector_momentum_bonus分
          3. 板块指数收盘价 > 20日均线 → +sector_above_ma_bonus分
          4. 总分不超过sector_boost_weight
        """
        if not self.cfg.get('sector_boost_enabled', False):
            etf_scores_df['sector_boost'] = 0
            return etf_scores_df
        
        if sector_scores_df is None or sector_scores_df.empty:
            etf_scores_df['sector_boost'] = 0
            return etf_scores_df
        
        # 构建映射：ETF ticker -> 板块 ticker
        mapping = {k: f"SECTOR_{v}" for k, v in ETF_TO_SECTOR_MAPPING.items()}
        
        sector_scores_df = sector_scores_df.copy()
        reverse_mapping = {v: k for k, v in mapping.items()}
        sector_scores_df['etf_ticker'] = sector_scores_df['ticker'].map(reverse_mapping)
        sector_scores_df = sector_scores_df[sector_scores_df['etf_ticker'].notna()].copy()
        
        if sector_scores_df.empty:
            etf_scores_df['sector_boost'] = 0
            return etf_scores_df
        
        # 按日期计算板块排名
        sector_boost_list = []
        
        for date in etf_scores_df['date'].unique():
            day_etf = etf_scores_df[etf_scores_df['date'] == date].copy()
            day_sector = sector_scores_df[sector_scores_df['date'] == date].copy()
            
            if day_sector.empty:
                day_etf['sector_boost'] = 0
                sector_boost_list.append(day_etf)
                continue
            
            # 计算板块排名
            day_sector = day_sector.sort_values('total_score', ascending=False)
            day_sector['sector_rank'] = range(1, len(day_sector) + 1)
            
            sector_momentum_bonus = self.cfg.get('sector_momentum_bonus', 10)
            sector_above_ma_bonus = self.cfg.get('sector_above_ma_bonus', 5)
            sector_top_n = self.cfg.get('sector_top_n_threshold', 3)
            max_boost = self.cfg.get('sector_boost_weight', 15)
            
            boost_map = {}
            for _, row in day_sector.iterrows():
                etf_ticker = row['etf_ticker']
                boost = 0
                
                if row['sector_rank'] <= sector_top_n:
                    boost += sector_momentum_bonus
                
                if row['close'] > row['ma20']:
                    boost += sector_above_ma_bonus
                
                boost_map[etf_ticker] = min(boost, max_boost)
            
            day_etf['sector_boost'] = day_etf['ticker'].map(boost_map).fillna(0)
            sector_boost_list.append(day_etf)
        
        result = pd.concat(sector_boost_list, ignore_index=True)
        return result
    
    def market_timing(self, bench_df: pd.DataFrame) -> pd.DataFrame:
        """
        大盘择时：根据沪深300趋势确定总仓位上限
        
        Returns:
            DataFrame with 'market_signal' column: 1.0(满仓) / 0.5(半仓) / 0.2(防御)
        """
        df = bench_df.copy().sort_values('date')
        
        # 计算大盘均线（用前一日）
        df['bench_ma20'] = df['close'].rolling(self.cfg['market_ma_short']).mean().shift(1)
        df['bench_ma50'] = df['close'].rolling(self.cfg['market_ma_long']).mean().shift(1)
        
        # 大盘信号
        df['market_signal'] = 1.0  # 默认满仓
        
        # 收盘价在20-50日均线之间 -> 半仓
        mask_half = (df['close'].shift(1) <= df['bench_ma20']) & (df['close'].shift(1) > df['bench_ma50'])
        df.loc[mask_half, 'market_signal'] = 0.5
        
        # 收盘价跌破50日均线 -> 防御仓位20%
        mask_defense = df['close'].shift(1) <= df['bench_ma50']
        df.loc[mask_defense, 'market_signal'] = 0.2
        
        return df
    
    def generate_signals(self, scores_df: pd.DataFrame, bench_df: pd.DataFrame) -> pd.DataFrame:
        """
        生成交易信号
        
        入场条件（必须同时满足）：
          1. 趋势得分 ≥ 15
          2. 确认得分 ≥ 4（至少1天在均线之上）
          3. 总评分 ≥ 40
          4. 前一日收盘价 > 20日均线 且 均线斜率 > 0
        
        出场条件（任一满足）：
          1. 前一日收盘价跌破20日均线
        """
        # 合并大盘择时信号
        if self.cfg['market_timing'] and bench_df is not None:
            bench_signals = self.market_timing(bench_df)[['date', 'market_signal']]
            scores_df = scores_df.merge(bench_signals, on='date', how='left')
            scores_df['market_signal'] = scores_df['market_signal'].fillna(1.0)
        else:
            scores_df['market_signal'] = 1.0
        
        # 使用groupby防止跨ETF污染的shift
        scores_df['prev_close'] = scores_df.groupby('ticker')['close'].shift(1)
        
        # 入场信号
        scores_df['signal_type'] = 'HOLD'
        
        buy_mask = (
            (scores_df['trend_score'] >= self.cfg['min_trend_score']) &
            (scores_df['confirm_score'] >= self.cfg['min_confirm_score']) &
            (scores_df['total_score'] >= self.cfg['min_total_score']) &
            (scores_df['prev_close'] > scores_df['ma20']) &
            (scores_df['ma20_slope'] > 0)
        )
        
        scores_df.loc[buy_mask, 'signal_type'] = 'BUY'
        
        # 出场信号：前一日收盘价跌破均线
        sell_mask = scores_df['prev_close'] < scores_df['ma20']
        scores_df.loc[sell_mask, 'signal_type'] = 'SELL'
        
        return scores_df
    
    def get_latest_signals(self, db) -> pd.DataFrame:
        """从数据库获取最新信号"""
        latest = db.get_latest_date()
        if not latest:
            return pd.DataFrame()
        
        scores = db.get_scores(date=latest)
        if scores.empty:
            return pd.DataFrame()
        
        # 筛选买入信号
        buy_signals = scores[
            (scores['trend_score'] >= self.cfg['min_trend_score']) &
            (scores['confirm_score'] >= self.cfg['min_confirm_score']) &
            (scores['total_score'] >= self.cfg['min_total_score'])
        ].sort_values('total_score', ascending=False)
        
        return buy_signals


if __name__ == '__main__':
    # 测试策略引擎
    engine = StrategyEngine()
    print("策略引擎初始化完成")
    print(f"配置: {json.dumps(engine.cfg, indent=2, ensure_ascii=False)}")

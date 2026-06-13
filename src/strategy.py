"""
策略引擎 v1.1 - ETF轮动评分体系（防御型交易规则版本）
原始参数（表现最优）：
  趋势30% + 确认20% + 动量25% + 成交量15% + 波动率10%

版本定位：
  v1.1 = "防御型交易规则版本"
  核心内容：基础评分体系 + 防御资产评分（简化版）
  
不包含：
  - 行业板块指数数据（移到 v1.2/v1.3 信号增强版本）
  - 板块动量增强（移到 v1.2）

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

from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, BENCHMARK


class StrategyEngine:
    """ETF轮动策略引擎"""
    
    def __init__(self, cfg=None):
        self.cfg = cfg or STRATEGY_CONFIG
        self.tickers = list(ETF_UNIVERSE.keys())
        self.benchmark = BENCHMARK
        # 三类资产分类
        self.stock_tickers = list(ETF_UNIVERSE.keys())
        self.fallback_tickers = list(FALLBACK_EQUITY_UNIVERSE.keys())
        self.defense_tickers = list(DEFENSE_UNIVERSE.keys())
        self.all_tickers = self.stock_tickers + self.fallback_tickers + self.defense_tickers
    
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
    
    def calculate_total_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算总评分（单ETF调用，不含横截面动量排名）
        
        注意：此方法不计算横截面动量排名，因为单只ETF无法做横截面比较。
        横截面排名需调用rank_all_momentum（全universe合并后）。
        
        这是backtest.py run()中逐ETF调用的接口。
        """
        return self.calculate_scores(df)
    
    def calculate_defense_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        防御资产简化评分（不参与横截面动量排名）
        
        防御资产（黄金/国债）不参与日常轮动，只在防御仓位时配置。
        简化评分逻辑：
          - 趋势强度：价>MA20(+15) + 价>MA50(+10) + MA20斜率>0(+5) = 30分
          - 动量排名：固定12.5分（不参与排名）
          - 总评分 = 趋势 + 确认 + 动量固定分
        
        入场门槛比股票ETF低，确保熊市中更容易达标。
        """
        df = self.calculate_indicators(df)
        
        # 1. 趋势强度 (30分)
        df['trend_score'] = 0
        df.loc[df['close'].shift(1) > df['ma20'], 'trend_score'] += 15
        df.loc[df['close'].shift(1) > df['ma50'], 'trend_score'] += 10
        df.loc[df['ma20_slope'] > 0, 'trend_score'] += 5
        
        # 2. 趋势确认 (20分) - 连续在均线之上的天数
        df['confirm_score'] = np.minimum(df['above_ma20_days'] * 4, 20)
        
        # 3. 动量：固定12.5分（不参与横截面排名）
        df['momentum_rank'] = 12.5
        df['momentum_valid'] = True
        
        # 4. 成交量：防御资产不依赖成交量，给默认5分
        df['volume_score'] = 5
        df['volume_ratio'] = 1.0
        
        # 5. 波动率：防御资产偏好低波动，适中波动给10分
        df['vol_score'] = 10
        vol = df['volatility_20'].abs()
        df.loc[vol > 0.10, 'vol_score'] = 5  # 波动过高减分
        
        return df
    
    def calculate_fallback_equity_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        宽基补仓ETF简化评分（不参与横截面动量排名）
        
        宽基ETF（沪深300/中证500/创业板/科创50）不参与日常轮动排名。
        当行业ETF买不满时，用简化趋势条件判断宽基是否可以补仓：
          - 收盘价 > MA20 或 MA50（至少站上一条均线）
          - MA20斜率不急剧向下（> -0.01）
          - 总评分达到较宽松阈值（默认25分）
        
        简化评分逻辑：
          - 趋势强度：价>MA20(+15) + 价>MA50(+10) + MA20斜率>0(+5) = 30分
          - 动量排名：固定12.5分（不参与排名）
          - 总评分 = 趋势 + 确认 + 动量固定分
        """
        df = self.calculate_indicators(df)
        
        # 1. 趋势强度 (30分) - 只要站上一条均线就给分
        df['trend_score'] = 0
        df.loc[df['close'].shift(1) > df['ma20'], 'trend_score'] += 15
        df.loc[df['close'].shift(1) > df['ma50'], 'trend_score'] += 10
        df.loc[df['ma20_slope'] > 0, 'trend_score'] += 5
        
        # 2. 趋势确认 (20分) - 连续在均线之上的天数
        df['confirm_score'] = np.minimum(df['above_ma20_days'] * 4, 20)
        
        # 3. 动量：固定12.5分（不参与横截面排名）
        df['momentum_rank'] = 12.5
        df['momentum_valid'] = True
        
        # 4. 成交量：给默认5分
        df['volume_score'] = 5
        df['volume_ratio'] = 1.0
        
        # 5. 波动率：适中波动给10分
        df['vol_score'] = 10
        vol = df['volatility_20'].abs()
        df.loc[vol > 0.15, 'vol_score'] = 5  # 波动过高减分
        
        return df
    
    def market_timing(self, bench_df: pd.DataFrame) -> pd.DataFrame:
        """
        大盘择时：根据沪深300趋势确定总仓位上限
        
        优化版(v1.1)：只用单均线(ma50)，避免牛市中正常回调触发半仓
        原三档(1.0/0.5/0.2)触发太频繁，改为两档更简洁：
          - 1.0(满仓): close > ma50（中期趋势向上）
          - 0.5(半仓): close <= ma50（跌破中期趋势）
        
        Returns:
            DataFrame with 'market_signal' column: 1.0(满仓) / 0.5(半仓)
        """
        df = bench_df.copy().sort_values('date')
        
        # 计算大盘50日均线（用前一日，避免未来信息泄露）
        df['bench_ma50'] = df['close'].rolling(self.cfg['market_ma_long']).mean().shift(1)
        
        # 两档信号：只在明确跌破 ma50 时降低仓位
        df['market_signal'] = 1.0  # 默认满仓
        
        # 收盘价跌破50日均线 -> 半仓（不再到0.2的极端防御）
        mask_reduce = df['close'].shift(1) <= df['bench_ma50']
        df.loc[mask_reduce, 'market_signal'] = 0.5
        
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
        
        # 计算大盘强势标志（用于宽基补仓判断）
        # 大盘强势 = 收盘价 > MA50 且 MA50斜率 > 0
        # 这个条件独立于 market_timing，专门用于宽基补仓的"时机"判断
        # 只有在大盘趋势向上时，才允许用宽基补仓，避免弱势反弹时追涨
        if bench_df is not None and not bench_df.empty:
            bench_sorted = bench_df.sort_values('date').copy()
            bench_sorted['bench_ma50'] = bench_sorted['close'].rolling(self.cfg['market_ma_long']).mean().shift(1)
            bench_sorted['bench_ma50_slope'] = bench_sorted['bench_ma50'].diff().shift(1)
            bench_sorted['bull_market'] = (
                (bench_sorted['close'].shift(1) > bench_sorted['bench_ma50']) &
                (bench_sorted['bench_ma50_slope'] > 0)
            )
            scores_df = scores_df.merge(bench_sorted[['date', 'bull_market']], on='date', how='left')
            scores_df['bull_market'] = scores_df['bull_market'].fillna(False)
        else:
            scores_df['bull_market'] = True  # 无基准数据时默认允许（向后兼容）
        
        buy_mask = (
            (scores_df['trend_score'] >= self.cfg['min_trend_score']) &
            (scores_df['confirm_score'] >= self.cfg['min_confirm_score']) &
            (scores_df['total_score'] >= self.cfg['min_total_score']) &
            (scores_df['prev_close'] > scores_df['ma20']) &
            (scores_df['ma20_slope'] > 0)
        )
        
        # 宽基补仓ETF：只在大盘强势时触发，补足beta
        import config as _cfg_module
        _fallback_tickers = list(getattr(_cfg_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
        fallback_mask = scores_df['ticker'].isin(_fallback_tickers) & scores_df['bull_market'] & (
            (scores_df['trend_score'] >= 10) &  # 比股票ETF低5分
            (scores_df['confirm_score'] >= 2) &  # 比股票ETF低2分
            (scores_df['total_score'] >= 25) &   # 比股票ETF低15分
            (scores_df['prev_close'] > scores_df['ma20'] * 0.98) &  # 允许2%缓冲
            (scores_df['ma20_slope'] > -0.01)   # 允许均线轻微向下
        )
        
        # 防御资产更宽松的入场条件（低相关补仓，不依赖大盘强势）
        _defense_tickers = list(_cfg_module.DEFENSE_UNIVERSE.keys())
        defense_mask = scores_df['ticker'].isin(_defense_tickers) & (
            (scores_df['trend_score'] >= 10) &  # 比股票ETF低5分
            (scores_df['confirm_score'] >= 2) &  # 比股票ETF低2分
            (scores_df['total_score'] >= 30) &   # 比股票ETF低10分
            (scores_df['prev_close'] > scores_df['ma20'] * 0.98) &  # 允许2%缓冲
            (scores_df['ma20_slope'] > -0.001)   # 允许均线微跌
        )
        
        scores_df.loc[buy_mask | fallback_mask | defense_mask, 'signal_type'] = 'BUY'
        
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

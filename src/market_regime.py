"""
市场状态检测模块 v1.2 - Market Regime Detection

基于沪深300指数趋势、斜率、波动率、市场宽度等多维度指标，
检测当前市场处于强牛/弱牛/震荡/熊市四种状态之一。

v1.2 采用 observer-first 策略：
- 检测状态并记录，但不自动改变交易参数（mode='observer'）
- 状态用于 CLI/UI 展示、回测报告分析、后续版本参数映射

指标来源：
- 沪深300指数：MA20/MA50/斜率/波动率
- 行业ETF宽度：16只行业ETF中收盘价>MA50的比例（预留，可替换为板块指数）

状态切换规则：
- 连续 confirmation_days（默认5）个交易日满足条件才确认切换
- 避免噪声导致的频繁切换

状态定义：
1. 强牛：沪深300 > MA50×1.02，MA50斜率加速，波动率适中
2. 弱牛：沪深300 > MA50×1.02，但斜率减速或波动率异常
3. 震荡：沪深300在MA20-MA50之间，或波动率极低
4. 熊市：沪深300 < MA50×0.98，MA50斜率加速向下
"""

import pandas as pd
import numpy as np
from datetime import datetime


class MarketRegimeDetector:
    """市场状态检测器"""
    
    STATE_NAMES = {
        1: '强牛',
        2: '弱牛',
        3: '震荡',
        4: '熊市',
    }
    
    def __init__(self, cfg=None):
        """
        初始化检测器
        
        Args:
            cfg: 配置字典，可选字段：
                - confirmation_days: 状态切换确认天数（默认5）
                - ma_short: 短期均线（默认20）
                - ma_long: 长期均线（默认50）
                - trend_position_threshold_strong: 强牛趋势位置阈值（默认1.02）
                - trend_position_threshold_weak: 震荡下限（默认0.98）
                - vol_low_threshold: 低波动率阈值（默认0.015）
                - vol_high_threshold: 高波动率阈值（默认0.025）
                - slope_accel_threshold: 斜率加速阈值（默认1.0）
        """
        self.cfg = cfg or {}
        self.confirmation_days = self.cfg.get('confirmation_days', 5)
        self.ma_short = self.cfg.get('ma_short', 20)
        self.ma_long = self.cfg.get('ma_long', 50)
        self.trend_pos_strong = self.cfg.get('trend_position_threshold_strong', 1.02)
        self.trend_pos_weak = self.cfg.get('trend_position_threshold_weak', 0.98)
        self.vol_low = self.cfg.get('vol_low_threshold', 0.015)
        self.vol_high = self.cfg.get('vol_high_threshold', 0.025)
        self.slope_accel_thresh = self.cfg.get('slope_accel_threshold', 1.0)
        
        # 状态历史（用于连续确认）
        self.state_history = []
        self.current_state = 3  # 默认震荡
    
    def calculate_indicators(self, bench_df: pd.DataFrame, market_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        计算市场状态所需指标
        
        Args:
            bench_df: 沪深300日K数据（含date, close, open, high, low, volume）
            market_df: 可选，行业ETF日K数据（含date, ticker, close），用于计算市场宽度
        
        Returns:
            DataFrame with regime indicators
        """
        df = bench_df.copy().sort_values('date').reset_index(drop=True)
        
        # 均线（用前一日收盘计算，避免未来信息泄露）
        df['ma20'] = df['close'].rolling(self.ma_short).mean().shift(1)
        df['ma50'] = df['close'].rolling(self.ma_long).mean().shift(1)
        
        # 均线斜率（当日斜率 = 当日MA50 - 昨日MA50，用前一日数据）
        df['ma50_slope'] = df['ma50'].diff().shift(1)
        
        # 斜率加速度 = 当前斜率 / 20天前的斜率
        # 正确计算：用 ma50_slope.shift(20) 获取20天前的日度斜率
        df['ma50_slope_20'] = df['ma50_slope'].shift(self.ma_short)
        
        # 斜率加速度：当前斜率相对于20天前的变化倍数
        # >1.0 表示斜率在加速（趋势增强），<1.0 表示斜率在减速（趋势减弱）
        df['slope_accel'] = df['ma50_slope'] / df['ma50_slope_20']
        df['slope_accel'] = df['slope_accel'].replace([np.inf, -np.inf], np.nan)
        # 当20天前斜率接近0时，加速度可能异常，做截断处理
        df['slope_accel'] = df['slope_accel'].clip(-10, 10)
        # 斜率同号时才计算加速度（异号意味着趋势反转，直接判定为减速）
        df.loc[(df['ma50_slope'] > 0) & (df['ma50_slope_20'] < 0), 'slope_accel'] = -1.0
        df.loc[(df['ma50_slope'] < 0) & (df['ma50_slope_20'] > 0), 'slope_accel'] = -1.0
        
        # 趋势位置：收盘价相对 MA50
        df['trend_position'] = df['close'].shift(1) / df['ma50']
        
        # 短期位置：收盘价相对 MA20
        df['short_position'] = df['close'].shift(1) / df['ma20']
        
        # 20日波动率（用前一日数据）
        df['vol_20'] = df['close'].pct_change().rolling(self.ma_short).std().shift(1)
        
        # 市场宽度（如果有行业ETF数据）
        if market_df is not None and not market_df.empty:
            market_breadth = self._calculate_market_breadth(market_df)
            df = df.merge(market_breadth[['date', 'market_breadth']], on='date', how='left')
        else:
            df['market_breadth'] = np.nan
        
        return df
    
    def _calculate_market_breadth(self, market_df: pd.DataFrame) -> pd.DataFrame:
        """
        计算市场宽度：行业ETF中收盘价 > MA50 的比例
        
        v1.2 使用现有 16 只行业ETF计算宽度。
        v1.3 可替换为板块指数宽度。
        """
        df = market_df.copy().sort_values(['ticker', 'date']).reset_index(drop=True)
        
        # 每只ETF的MA50（用前一日）
        df['ma50'] = df.groupby('ticker')['close'].transform(
            lambda x: x.rolling(self.ma_long).mean().shift(1)
        )
        
        # 收盘价是否高于MA50（用前一日收盘，按 ticker 分组避免跨 ETF 污染）
        df['prev_close'] = df.groupby('ticker')['close'].shift(1)
        df['above_ma50'] = (df['prev_close'] > df['ma50']).astype(int)
        
        # 过滤 MA50 不足的早期数据（NaN 不应当 0 参与平均）
        df_valid = df.dropna(subset=['ma50', 'prev_close'])
        
        # 每日宽度 = 高于MA50的ETF比例（只统计有效数据）
        breadth = df_valid.groupby('date')['above_ma50'].mean().reset_index()
        breadth.columns = ['date', 'market_breadth']
        
        return breadth
    
    def _classify_volatility(self, vol_20: float) -> str:
        """分类波动率：low / med / high"""
        if pd.isna(vol_20):
            return 'med'
        if vol_20 < self.vol_low:
            return 'low'
        if vol_20 > self.vol_high:
            return 'high'
        return 'med'
    
    def _determine_state(self, trend_position: float, ma50_slope: float,
                         slope_accel: float, vol_regime: str) -> int:
        """
        根据指标判定当前状态
        
        状态定义：
        1=强牛, 2=弱牛, 3=震荡, 4=熊市
        """
        # 强牛：趋势强 + 斜率加速 + 波动率不极端（low或med均可，排除high）
        if (trend_position > self.trend_pos_strong and
            ma50_slope > 0 and
            slope_accel > self.slope_accel_thresh and
            vol_regime != 'high'):
            return 1
        
        # 弱牛：趋势强但斜率减速或波动率异常
        if (trend_position > self.trend_pos_strong and
            ma50_slope > 0 and
            (slope_accel <= self.slope_accel_thresh or vol_regime == 'high')):
            return 2
        
        # 震荡：趋势中性
        if (trend_position > self.trend_pos_weak and
            ma50_slope > -0.001):
            return 3
        
        # 熊市：趋势弱
        return 4
    
    def _generate_reason(self, state: int, trend_position: float,
                         short_position: float, ma50_slope: float,
                         slope_accel: float, vol_regime: str, vol_20: float,
                         market_breadth: float) -> str:
        """生成状态原因说明"""
        reasons = []
        
        if state == 1:
            reasons.append(f"沪深300高于MA50 {trend_position:.2%}，中期趋势向上")
            reasons.append(f"MA50斜率加速（{slope_accel:.2f}倍）")
            reasons.append("波动率适中")
        elif state == 2:
            reasons.append(f"沪深300高于MA50 {trend_position:.2%}，但趋势强度减弱")
            if slope_accel <= self.slope_accel_thresh:
                reasons.append(f"MA50斜率减速（{slope_accel:.2f}倍）")
            if vol_regime in ['high', 'low']:
                reasons.append(f"波动率{vol_regime}（{vol_20:.2%}）")
        elif state == 3:
            if short_position > 1.0:
                reasons.append(f"沪深300高于MA20但接近MA50（相对MA50: {trend_position:.2%}）")
            else:
                reasons.append(f"沪深300在MA20下方或附近（相对MA50: {trend_position:.2%}）")
            if ma50_slope > 0:
                reasons.append("MA50斜率仍为正但接近0")
            else:
                reasons.append("MA50斜率接近0或微负")
            if vol_regime == 'low':
                reasons.append("低波动率，缺乏方向")
        elif state == 4:
            reasons.append(f"沪深300低于MA50 {trend_position:.2%}，中期趋势向下")
            reasons.append(f"MA50斜率向下（{ma50_slope:.4f}）")
        
        # 市场宽度补充
        if not pd.isna(market_breadth):
            if market_breadth > 0.6:
                reasons.append(f"市场宽度高（{market_breadth:.0%} 行业ETF站上MA50）")
            elif market_breadth < 0.3:
                reasons.append(f"市场宽度低（{market_breadth:.0%} 行业ETF站上MA50）")
        
        return "；".join(reasons)
    
    def _calculate_confidence(self, state: int, trend_position: float,
                              ma50_slope: float, slope_accel: float,
                              vol_regime: str, market_breadth: float) -> float:
        """
        计算状态置信度（0.1 - 0.95）
        
        基于指标与状态定义的一致性程度计算。
        """
        score = 0.5
        
        # 趋势位置确定性（距离阈值越远，置信度越高）
        if state in [1, 2]:
            score += min(0.2, (trend_position - self.trend_pos_strong) * 5)
        elif state == 4:
            score += min(0.2, (self.trend_pos_weak - trend_position) * 5)
        
        # 斜率确定性
        if ma50_slope > 0:
            score += min(0.15, ma50_slope * 100)
        elif ma50_slope < -0.001:
            score += min(0.15, abs(ma50_slope) * 100)
        
        # 斜率加速度（偏离1.0越远，趋势越明确）
        if not pd.isna(slope_accel):
            if slope_accel > 1.2 or slope_accel < 0.8:
                score += 0.1
        
        # 市场宽度一致性（宽度与状态一致则加分，不一致则减分）
        if not pd.isna(market_breadth):
            if (state in [1, 2] and market_breadth > 0.5) or (state == 4 and market_breadth < 0.3):
                score += 0.1
            elif (state in [1, 2] and market_breadth < 0.3) or (state == 4 and market_breadth > 0.5):
                score -= 0.1
        
        # 波动率一致性（极端波动率降低置信度）
        if vol_regime == 'high':
            score -= 0.05
        
        return min(0.95, max(0.1, score))
    
    def detect(self, bench_df: pd.DataFrame, market_df: pd.DataFrame = None) -> dict:
        """
        检测当前（最新）市场状态
        
        Args:
            bench_df: 沪深300日K数据
            market_df: 可选，行业ETF日K数据
        
        Returns:
            dict: {
                'regime_id': int,          # 1=强牛, 2=弱牛, 3=震荡, 4=熊市
                'regime_name': str,        # 中文名称
                'confirmed': bool,         # 是否刚确认状态切换
                'raw_state': int,          # 原始检测状态（未确认）
                'confidence': float,       # 置信度 0-1
                'reason': str,             # 状态原因说明
                'trend_position': float,   # 趋势位置
                'short_position': float,   # 短期位置
                'ma50_slope': float,       # MA50斜率
                'slope_accel': float,      # 斜率加速度
                'vol_20': float,           # 20日波动率
                'vol_regime': str,         # 波动率分类
                'market_breadth': float,   # 市场宽度
                'date': datetime,          # 最新日期
            }
        """
        df = self.calculate_indicators(bench_df, market_df)
        
        # 取最新一行（有有效数据的）
        valid = df.dropna(subset=['trend_position', 'ma50_slope'])
        if valid.empty:
            return {
                'regime_id': self.current_state,
                'regime_name': self.STATE_NAMES[self.current_state],
                'confirmed': False,
                'raw_state': self.current_state,
                'confidence': 0.5,
                'reason': '数据不足，无法检测',
                'date': df['date'].iloc[-1] if not df.empty else None,
            }
        
        latest = valid.iloc[-1]
        
        # 提取指标
        trend_position = latest['trend_position']
        short_position = latest['short_position']
        ma50_slope = latest['ma50_slope']
        slope_accel = latest['slope_accel']
        vol_20 = latest['vol_20']
        market_breadth = latest.get('market_breadth', np.nan)
        
        vol_regime = self._classify_volatility(vol_20)
        
        # 状态判定
        new_state = self._determine_state(trend_position, ma50_slope, slope_accel, vol_regime)
        
        # 连续确认
        self.state_history.append(new_state)
        if len(self.state_history) > self.confirmation_days:
            self.state_history.pop(0)
        
        confirmed = False
        if (len(self.state_history) == self.confirmation_days and
            len(set(self.state_history)) == 1):
            if self.current_state != new_state:
                self.current_state = new_state
                confirmed = True
        
        # 生成原因和置信度
        reason = self._generate_reason(
            self.current_state, trend_position, short_position,
            ma50_slope, slope_accel, vol_regime, vol_20, market_breadth
        )
        confidence = self._calculate_confidence(
            self.current_state, trend_position, ma50_slope, slope_accel, vol_regime, market_breadth
        )
        
        return {
            'regime_id': self.current_state,
            'regime_name': self.STATE_NAMES[self.current_state],
            'confirmed': confirmed,
            'raw_state': new_state,
            'confidence': confidence,
            'reason': reason,
            'trend_position': trend_position,
            'short_position': short_position,
            'ma50_slope': ma50_slope,
            'slope_accel': slope_accel,
            'vol_20': vol_20,
            'vol_regime': vol_regime,
            'market_breadth': market_breadth,
            'date': latest['date'],
        }
    
    def detect_history(self, bench_df: pd.DataFrame, market_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        检测全历史状态序列
        
        逐日检测并记录状态，返回完整历史状态DataFrame。
        用于回测报告分析和状态分布统计。
        
        Args:
            bench_df: 沪深300日K数据
            market_df: 可选，行业ETF日K数据
        
        Returns:
            DataFrame with columns: date, regime_id, regime_name, confidence, reason, ...
        """
        df = self.calculate_indicators(bench_df, market_df)
        
        # 重置状态
        self.state_history = []
        self.current_state = 3
        
        results = []
        
        for i in range(len(df)):
            row = df.iloc[i]
            
            # 跳过数据不足的行
            if pd.isna(row['trend_position']) or pd.isna(row['ma50_slope']):
                continue
            
            trend_position = row['trend_position']
            short_position = row['short_position']
            ma50_slope = row['ma50_slope']
            slope_accel = row['slope_accel']
            vol_20 = row['vol_20']
            market_breadth = row.get('market_breadth', np.nan)
            
            vol_regime = self._classify_volatility(vol_20)
            
            # 状态判定
            new_state = self._determine_state(trend_position, ma50_slope, slope_accel, vol_regime)
            
            # 连续确认
            self.state_history.append(new_state)
            if len(self.state_history) > self.confirmation_days:
                self.state_history.pop(0)
            
            confirmed = False
            if (len(self.state_history) == self.confirmation_days and
                len(set(self.state_history)) == 1):
                if self.current_state != new_state:
                    self.current_state = new_state
                    confirmed = True
            
            # 生成原因和置信度
            reason = self._generate_reason(
                self.current_state, trend_position, short_position,
                ma50_slope, slope_accel, vol_regime, vol_20, market_breadth
            )
            confidence = self._calculate_confidence(
                self.current_state, trend_position, ma50_slope, slope_accel, vol_regime, market_breadth
            )
            
            results.append({
                'date': row['date'],
                'regime_id': self.current_state,
                'regime_name': self.STATE_NAMES[self.current_state],
                'confirmed_switch': confirmed,
                'raw_state': new_state,
                'confidence': confidence,
                'reason': reason,
                'trend_position': trend_position,
                'short_position': short_position,
                'ma50_slope': ma50_slope,
                'slope_accel': slope_accel,
                'vol_20': vol_20,
                'vol_regime': vol_regime,
                'market_breadth': market_breadth,
            })
        
        return pd.DataFrame(results)
    
    def get_state_summary(self, history_df: pd.DataFrame) -> dict:
        """
        生成状态分布摘要
        
        Args:
            history_df: detect_history() 返回的 DataFrame
        
        Returns:
            dict: 状态分布统计
        """
        if history_df.empty:
            return {}
        
        total_days = len(history_df)
        
        summary = {
            'total_days': total_days,
            'date_range': f"{history_df['date'].min()} to {history_df['date'].max()}",
            'state_distribution': {},
            'switch_count': history_df['confirmed_switch'].sum(),
            'avg_confidence': history_df['confidence'].mean(),
        }
        
        for state_id, state_name in self.STATE_NAMES.items():
            mask = history_df['regime_id'] == state_id
            count = mask.sum()
            pct = count / total_days if total_days > 0 else 0
            avg_conf = history_df.loc[mask, 'confidence'].mean() if count > 0 else 0
            
            summary['state_distribution'][state_id] = {
                'name': state_name,
                'days': int(count),
                'percentage': float(pct),
                'avg_confidence': float(avg_conf) if not pd.isna(avg_conf) else 0,
            }
        
        # 按年度统计
        history_df['year'] = pd.to_datetime(history_df['date']).dt.year
        yearly = {}
        for year, group in history_df.groupby('year'):
            yearly[int(year)] = {
                state_id: int((group['regime_id'] == state_id).sum())
                for state_id in self.STATE_NAMES.keys()
            }
        summary['yearly_distribution'] = yearly
        
        return summary


if __name__ == '__main__':
    # 测试市场状态检测器
    print("市场状态检测模块初始化完成")
    detector = MarketRegimeDetector()
    print(f"默认状态: {detector.STATE_NAMES[detector.current_state]}")
    print(f"确认天数: {detector.confirmation_days}")

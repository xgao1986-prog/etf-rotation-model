"""
回测引擎 - ETF轮动策略回测
支持：交易费率、止损、仓位控制、大盘择时
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json

from config import STRATEGY_CONFIG, ETF_UNIVERSE, BACKTEST_CONFIG
from strategy import StrategyEngine


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, cfg=None):
        self.cfg = cfg or STRATEGY_CONFIG
        self.strategy = StrategyEngine(self.cfg)
        self.initial_capital = BACKTEST_CONFIG['initial_capital']
    
    def run(self, market_df: pd.DataFrame, bench_df: pd.DataFrame, sector_df=None) -> dict:
        """
        运行回测
        
        正确评分路径：
        1. 逐ETF计算指标和评分（不含动量排名）
        2. 合并所有ETF的scores
        3. 按日期对全universe做momentum_20横截面排名
        4. 计算total_score（含板块增强，如启用）
        5. 生成信号（含大盘择时）
        6. 执行回测
        
        Parameters:
            market_df: 所有ETF的行情数据
            bench_df: 基准（沪深300）行情数据
            sector_df: 板块指数行情数据（可选，实验性v1.1）
        
        Returns:
            dict with backtest results
        """
        # 计算板块评分（实验性v1.1）
        sector_scores_df = None
        if sector_df is not None and not sector_df.empty and self.cfg.get('sector_boost_enabled', False):
            print("计算板块指数评分...")
            sector_scores_list = []
            for ticker in sector_df['ticker'].unique():
                sector_ticker_df = sector_df[sector_df['ticker'] == ticker].copy()
                if len(sector_ticker_df) < 50:
                    continue
                sector_scored = self.strategy.calculate_sector_total_score(sector_ticker_df)
                sector_scores_list.append(sector_scored)
            
            if sector_scores_list:
                sector_scores_df = pd.concat(sector_scores_list, ignore_index=True)
                print(f"板块评分计算完成: {len(sector_scores_df)} 条记录, {sector_scores_df['ticker'].nunique()} 个板块")
        
        # 步骤1-2：逐ETF计算指标和评分，合并
        all_scores = []
        for ticker in market_df['ticker'].unique():
            ticker_df = market_df[market_df['ticker'] == ticker].copy()
            if len(ticker_df) < 50:  # 数据不足跳过
                continue
            scored = self.strategy.calculate_total_score(ticker_df)
            all_scores.append(scored)
        
        if not all_scores:
            return {'error': '无有效数据'}
        
        scores_df = pd.concat(all_scores, ignore_index=True)
        
        # 步骤3：全universe横截面动量排名
        scores_df = self.strategy.rank_all_momentum(scores_df)
        
        # 步骤4：计算总评分（含板块增强）
        scores_df = self.strategy.compute_total_score(scores_df)
        
        # 添加板块动量增强（实验性v1.1）
        if sector_scores_df is not None:
            scores_df = self.strategy.calculate_sector_boost(scores_df, sector_scores_df)
            scores_df['total_score'] = scores_df['total_score'] + scores_df['sector_boost']
        
        # 步骤5：生成信号（含大盘择时合并）
        signals_df = self.strategy.generate_signals(scores_df, bench_df)
        
        # 步骤6：执行回测
        return self._execute_backtest(signals_df, market_df, bench_df)
    
    def _check_trailing_stop(self, pnl, drawdown) -> tuple:
        """检查是否触发动态止盈（实验性v1.2）"""
        mode = self.cfg.get('trailing_stop_mode', 'none')
        
        if mode == 'none':
            return False, ''
        
        if pnl <= 0:
            return False, ''
        
        if mode == 'simple':
            threshold = self.cfg.get('trailing_stop')
            if threshold is not None and drawdown < threshold:
                return True, f'移动止盈(回撤{threshold:.1%})'
            return False, ''
        
        elif mode == 'tiered':
            tiers = [
                (self.cfg.get('tier_3_pnl', 0.30), self.cfg.get('tier_3_drawdown', -0.12), '3档'),
                (self.cfg.get('tier_2_pnl', 0.15), self.cfg.get('tier_2_drawdown', -0.08), '2档'),
                (self.cfg.get('tier_1_pnl', 0.05), self.cfg.get('tier_1_drawdown', -0.05), '1档'),
            ]
            
            for pnl_threshold, dd_threshold, tier_name in tiers:
                if pnl >= pnl_threshold and drawdown < dd_threshold:
                    return True, f'分档止盈({tier_name}, 盈利{pnl:.1%}, 回撤{dd_threshold:.1%})'
            
            return False, ''
        
        return False, ''
    
    def _is_rebalance_day(self, date, last_rebalance_date=None) -> bool:
        """判断给定日期是否为调仓日（支持weekly/biweekly/monthly）"""
        dt = pd.to_datetime(date)
        weekday = dt.weekday()
        
        freq = self.cfg.get('rebalance_freq', 'weekly')
        target_weekday = self.cfg.get('rebalance_weekday', 4)
        
        if weekday != target_weekday:
            return False
        
        if freq == 'weekly':
            return True
        
        elif freq == 'biweekly':
            week_num = dt.isocalendar().week
            if last_rebalance_date is not None:
                days_since = (dt - pd.to_datetime(last_rebalance_date)).days
                return days_since >= 14
            else:
                return week_num % 2 == 1
        
        elif freq == 'monthly':
            ordinal = self.cfg.get('rebalance_ordinal', 1)
            
            if ordinal == -1:
                days_to_month_end = (dt + pd.offsets.MonthEnd(0) - dt).days
                return days_to_month_end < 7
            else:
                first_day_of_month = dt.replace(day=1)
                first_target_weekday = first_day_of_month + pd.Timedelta(
                    days=(target_weekday - first_day_of_month.weekday()) % 7
                )
                nth_target = first_target_weekday + pd.Timedelta(weeks=ordinal-1)
                return dt.date() == nth_target.date()
        
        return True
    
    def _execute_backtest(self, signals_df, market_df, bench_df) -> dict:
        """执行回测逻辑"""
        
        # 获取所有交易日
        dates = sorted(signals_df['date'].unique())
        
        # 初始化
        portfolio = {
            'cash': self.initial_capital,
            'positions': {},
            'total_value': self.initial_capital,
        }
        
        # 冷却期记录（实验性v1.2）
        cooling_list = {}
        
        nav_records = []
        trade_records = []
        last_rebalance_date = None
        
        # 佣金函数
        def calc_commission(amount):
            commission = max(amount * self.cfg['commission_rate'], self.cfg['min_commission'])
            return commission
        
        for i, date in enumerate(dates):
            date_str = pd.to_datetime(date).strftime('%Y-%m-%d')
            
            # 当日数据
            day_signals = signals_df[signals_df['date'] == date].copy()
            day_prices = market_df[market_df['date'] == date].set_index('ticker')['close'].to_dict()
            
            # 获取大盘择时信号
            max_total_position = 1.0
            if not day_signals.empty and 'market_signal' in day_signals.columns:
                max_total_position = day_signals['market_signal'].iloc[0]
            
            # ========== 每日止损检查（固定止损 + 动态止盈）==========
            stops = []
            for ticker, pos in list(portfolio['positions'].items()):
                if ticker in day_prices:
                    current_price = day_prices[ticker]
                    cost = pos.get('cost', current_price)
                    
                    # 更新最高价（动态止盈用）
                    high_water = pos.get('high_water', cost)
                    if current_price > high_water:
                        high_water = current_price
                        pos['high_water'] = high_water
                    
                    # 计算盈亏
                    pnl = (current_price - cost) / cost
                    drawdown = (current_price - high_water) / high_water
                    
                    # 层1：固定止损
                    if pnl < self.cfg['stop_loss']:
                        stops.append({
                            'ticker': ticker,
                            'current_price': current_price,
                            'cost': cost,
                            'high_water': high_water,
                            'pnl': pnl,
                            'drawdown': drawdown,
                            'entry_date': pos.get('entry_date', date_str),
                            'reason': '固定止损'
                        })
                    else:
                        # 层2：动态止盈（实验性v1.2）
                        triggered, stop_reason = self._check_trailing_stop(pnl, drawdown)
                        if triggered:
                            stops.append({
                                'ticker': ticker,
                                'current_price': current_price,
                                'cost': cost,
                                'high_water': high_water,
                                'pnl': pnl,
                                'drawdown': drawdown,
                                'entry_date': pos.get('entry_date', date_str),
                                'reason': stop_reason
                            })
            
            # 执行止损
            for stop in stops:
                ticker = stop['ticker']
                price = stop['current_price']
                pos = portfolio['positions'][ticker]
                shares = pos['shares']
                
                proceeds = shares * price
                commission = calc_commission(proceeds)
                net_proceeds = proceeds - commission
                
                portfolio['cash'] += net_proceeds
                
                trade_records.append({
                    'date': date_str,
                    'ticker': ticker,
                    'action': 'STOP_LOSS',
                    'price': price,
                    'shares': shares,
                    'amount': proceeds,
                    'commission': commission,
                    'pnl_pct': stop['pnl'],
                    'reason': f"{stop['reason']}: 成本盈亏{stop['pnl']:.2%}, 高点回撤{stop['drawdown']:.2%}"
                })
                
                # 记录冷却期（实验性v1.2）
                cooling_list[ticker] = date
                
                del portfolio['positions'][ticker]
            
            # ========== 调仓日检查（支持多种频率）==========
            is_rebalance = self._is_rebalance_day(date, last_rebalance_date)
            
            if is_rebalance:
                # 记录本次调仓日期
                last_rebalance_date = date
                
                # 1. 卖出不在候选列表的持仓
                buy_signals = day_signals[day_signals['signal_type'] == 'BUY'].sort_values(
                    'total_score', ascending=False
                )
                candidates = set(buy_signals['ticker'].tolist())
                
                for ticker in list(portfolio['positions'].keys()):
                    if ticker not in candidates:
                        if ticker in day_prices:
                            price = day_prices[ticker]
                            pos = portfolio['positions'][ticker]
                            shares = pos['shares']
                            
                            proceeds = shares * price
                            commission = calc_commission(proceeds)
                            net_proceeds = proceeds - commission
                            
                            portfolio['cash'] += net_proceeds
                            
                            pnl = (price - pos['cost']) / pos['cost']
                            trade_records.append({
                                'date': date_str,
                                'ticker': ticker,
                                'action': 'SELL',
                                'price': price,
                                'shares': shares,
                                'amount': proceeds,
                                'commission': commission,
                                'pnl_pct': pnl,
                                'reason': '调出候选列表'
                            })
                            
                            del portfolio['positions'][ticker]
                
                # 2. 买入新标的（考虑仓位控制 + 冷却期）
                current_holdings = len(portfolio['positions'])
                max_new = self.cfg['max_holdings'] - current_holdings
                
                if max_new > 0 and portfolio['cash'] > 1000:
                    # 计算当前组合净值
                    current_value = portfolio['cash'] + sum(
                        portfolio['positions'][t]['shares'] * day_prices.get(t, 0)
                        for t in portfolio['positions']
                    )
                    
                    # 根据大盘择时调整总仓位上限
                    target_total_value = current_value * max_total_position
                    
                    # 计算可用资金
                    available_cash = min(portfolio['cash'], target_total_value - (current_value - portfolio['cash']))
                    available_cash = max(available_cash, 0)
                    
                    # 每只标的的目标仓位
                    n_buy = min(max_new, len(buy_signals))
                    if n_buy > 0:
                        base_weight = min(self.cfg['max_position_per_etf'], 1.0 / n_buy)
                        
                        for _, row in buy_signals.head(n_buy).iterrows():
                            ticker = row['ticker']
                            
                            # 检查冷却期（实验性v1.2）
                            if ticker in cooling_list:
                                days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                                if days_since_stop < self.cfg.get('cooling_period', 0):
                                    continue  # 仍在冷却期内，跳过买入
                            
                            if ticker in day_prices and ticker not in portfolio['positions']:
                                price = day_prices[ticker]
                                
                                # 冷却期后重新买入需要更高评分（实验性v1.2）
                                min_score = self.cfg['min_total_score']
                                if ticker in cooling_list:
                                    days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                                    if days_since_stop >= self.cfg.get('cooling_period', 0):
                                        min_score += self.cfg.get('cooling_score_boost', 0)
                                
                                # 评分不达标则跳过
                                if row['total_score'] < min_score:
                                    continue
                                
                                # 目标金额 = 当前净值 × 单只权重 × 大盘择时仓位
                                target_amount = current_value * base_weight * max_total_position
                                target_amount = min(target_amount, available_cash * 0.95)
                                
                                if target_amount < 1000:
                                    continue
                                
                                shares = int(target_amount / price)
                                if shares < 1:
                                    continue
                                
                                cost = shares * price
                                commission = calc_commission(cost)
                                total_cost = cost + commission
                                
                                if total_cost > portfolio['cash']:
                                    continue
                                
                                portfolio['cash'] -= total_cost
                                available_cash -= total_cost
                                
                                portfolio['positions'][ticker] = {
                                    'shares': shares,
                                    'cost': price,
                                    'entry_date': date_str,
                                    'high_water': price  # 初始化最高价（动态止盈用）
                                }
                                
                                # 从冷却期列表中移除（已重新买入）
                                if ticker in cooling_list:
                                    del cooling_list[ticker]
                                
                                trade_records.append({
                                    'date': date_str,
                                    'ticker': ticker,
                                    'action': 'BUY',
                                    'price': price,
                                    'shares': shares,
                                    'amount': cost,
                                    'commission': commission,
                                    'pnl_pct': 0,
                                    'reason': f"评分{row['total_score']:.1f}"
                                })
            
            # ========== 计算当日净值 ==========
            positions_value = 0
            for ticker, pos in portfolio['positions'].items():
                if ticker in day_prices:
                    positions_value += pos['shares'] * day_prices[ticker]
            
            total_value = portfolio['cash'] + positions_value
            
            # 获取当日基准价格
            bench_price = None
            if not bench_df[bench_df['date'] == date].empty:
                bench_price = bench_df[bench_df['date'] == date]['close'].iloc[0]
            
            nav_records.append({
                'date': date_str,
                'nav': total_value,
                'cash': portfolio['cash'],
                'positions_value': positions_value,
                'num_positions': len(portfolio['positions']),
                'bench_price': bench_price,
                'max_total_position': max_total_position,
            })
        
        # ========== 计算绩效指标 ==========
        nav_df = pd.DataFrame(nav_records)
        if nav_df.empty:
            return {'error': '无回测结果'}
        
        nav_df['date'] = pd.to_datetime(nav_df['date'])
        nav_df = nav_df.sort_values('date')
        
        # 计算收益率
        nav_df['daily_return'] = nav_df['nav'].pct_change()
        nav_df['cumulative_return'] = (nav_df['nav'] / nav_df['nav'].iloc[0]) - 1
        
        # 基准收益率
        if nav_df['bench_price'].notna().any():
            first_bench = nav_df['bench_price'].dropna().iloc[0]
            nav_df['bench_return'] = (nav_df['bench_price'] / first_bench) - 1
        
        # 计算最大回撤
        nav_df['peak'] = nav_df['nav'].cummax()
        nav_df['drawdown'] = (nav_df['nav'] - nav_df['peak']) / nav_df['peak']
        
        # 绩效指标
        total_return = nav_df['cumulative_return'].iloc[-1]
        trading_days = len(nav_df)
        years = trading_days / 252
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 and total_return > -1 else 0
        
        daily_returns = nav_df['daily_return'].dropna()
        volatility = daily_returns.std() * np.sqrt(252)
        sharpe = annual_return / volatility if volatility > 0 else 0
        
        # 下行波动率 & 索提诺
        downside = daily_returns[daily_returns < 0].std() * np.sqrt(252)
        sortino = annual_return / downside if downside > 0 else 0
        
        max_drawdown = nav_df['drawdown'].min()
        
        # 交易统计
        trades_df = pd.DataFrame(trade_records)
        num_trades = len(trades_df)
        
        if not trades_df.empty and 'action' in trades_df.columns:
            win_trades = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS']) & (trades_df['pnl_pct'] > 0)]
            lose_trades = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS']) & (trades_df['pnl_pct'] <= 0)]
            win_rate = len(win_trades) / (len(win_trades) + len(lose_trades)) if (len(win_trades) + len(lose_trades)) > 0 else 0
            avg_win = win_trades['pnl_pct'].mean() if len(win_trades) > 0 else 0
            avg_loss = lose_trades['pnl_pct'].mean() if len(lose_trades) > 0 else 0
            total_commission = trades_df['commission'].sum()
            stop_loss_count = len(trades_df[trades_df['action'] == 'STOP_LOSS'])
        else:
            win_rate = 0
            avg_win = 0
            avg_loss = 0
            total_commission = 0
            stop_loss_count = 0
        
        result = {
            'nav_df': nav_df,
            'trades_df': trades_df,
            'total_return': total_return,
            'annual_return': annual_return,
            'volatility': volatility,
            'sharpe_ratio': sharpe,
            'sortino_ratio': sortino,
            'max_drawdown': max_drawdown,
            'num_trades': num_trades,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'total_commission': total_commission,
            'stop_loss_count': stop_loss_count,
            'avg_holdings': nav_df['num_positions'].mean(),
            'max_holdings': nav_df['num_positions'].max(),
            'params': self.cfg,
        }
        
        return result
    
    def run_in_sample(self, market_df, bench_df) -> dict:
        """样本内回测"""
        end = BACKTEST_CONFIG['in_sample_end']
        mask = market_df['date'] <= end
        return self.run(market_df[mask], bench_df[bench_df['date'] <= end])
    
    def run_out_sample(self, market_df, bench_df) -> dict:
        """样本外验证"""
        start = BACKTEST_CONFIG['out_sample_start']
        mask = market_df['date'] >= start
        return self.run(market_df[mask], bench_df[bench_df['date'] >= start])


if __name__ == '__main__':
    print("回测引擎初始化完成")
    print(f"初始资金: {BACKTEST_CONFIG['initial_capital']:,}")

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
        self.strategy = StrategyEngine(cfg)
        self.initial_capital = BACKTEST_CONFIG['initial_capital']
    
    def run(self, market_df: pd.DataFrame, bench_df: pd.DataFrame) -> dict:
        """
        运行回测
        
        Parameters:
            market_df: 所有ETF的行情数据
            bench_df: 基准（沪深300）行情数据
        
        Returns:
            dict with backtest results
        """
        # 计算评分
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
        
        # 生成信号
        signals_df = self.strategy.generate_signals(scores_df, bench_df)
        
        # 执行回测
        return self._execute_backtest(signals_df, market_df, bench_df)
    
    def _execute_backtest(self, signals_df, market_df, bench_df) -> dict:
        """执行回测逻辑"""
        
        # 获取所有交易日
        dates = sorted(signals_df['date'].unique())
        
        # 初始化
        portfolio = {
            'cash': self.initial_capital,
            'positions': {},  # {ticker: {'shares': x, 'cost': y, 'entry_date': z}}
            'total_value': self.initial_capital,
        }
        
        nav_records = []  # 净值记录
        trade_records = []  # 交易记录
        
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
            bench_day = bench_df[bench_df['date'] == date]
            max_total_position = 1.0
            if not bench_day.empty and 'market_signal' in bench_day.columns:
                max_total_position = bench_day['market_signal'].iloc[0]
            
            # ========== 每日止损检查 ==========
            stops = []
            for ticker, pos in list(portfolio['positions'].items()):
                if ticker in day_prices:
                    current_price = day_prices[ticker]
                    cost = pos.get('cost', current_price)
                    pnl = (current_price - cost) / cost
                    
                    if pnl < self.cfg['stop_loss']:
                        stops.append({
                            'ticker': ticker,
                            'current_price': current_price,
                            'cost': cost,
                            'pnl': pnl,
                            'entry_date': pos.get('entry_date', date_str)
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
                    'reason': f'止损触发: 亏损{stop["pnl"]:.2%}'
                })
                
                del portfolio['positions'][ticker]
            
            # ========== 调仓日检查（每周五） ==========
            is_rebalance = pd.to_datetime(date).weekday() == 4  # 周五
            
            if is_rebalance:
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
                
                # 2. 买入新标的（考虑仓位控制）
                current_holdings = len(portfolio['positions'])
                max_new = self.cfg['max_holdings'] - current_holdings
                
                if max_new > 0 and portfolio['cash'] > 1000:
                    # 根据大盘择时调整总仓位
                    target_total_value = self.initial_capital * max_total_position
                    current_value = portfolio['cash'] + sum(
                        portfolio['positions'][t]['shares'] * day_prices.get(t, 0)
                        for t in portfolio['positions']
                    )
                    
                    # 计算可用资金
                    available_cash = min(portfolio['cash'], 
                                        target_total_value - current_value + portfolio['cash'])
                    
                    # 每只标的的目标仓位
                    n_buy = min(max_new, len(buy_signals))
                    if n_buy > 0:
                        base_weight = min(self.cfg['max_position_per_etf'], 1.0 / n_buy)
                        
                        for _, row in buy_signals.head(n_buy).iterrows():
                            ticker = row['ticker']
                            if ticker in day_prices and ticker not in portfolio['positions']:
                                price = day_prices[ticker]
                                
                                # 目标金额
                                target_amount = self.initial_capital * base_weight * max_total_position
                                target_amount = min(target_amount, available_cash * 0.95)
                                
                                if target_amount < 1000:  # 最小交易金额
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
                                    'entry_date': date_str
                                }
                                
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
        
        win_trades = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS']) & (trades_df['pnl_pct'] > 0)]
        lose_trades = trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS']) & (trades_df['pnl_pct'] <= 0)]
        
        win_rate = len(win_trades) / (len(win_trades) + len(lose_trades)) if (len(win_trades) + len(lose_trades)) > 0 else 0
        
        avg_win = win_trades['pnl_pct'].mean() if len(win_trades) > 0 else 0
        avg_loss = lose_trades['pnl_pct'].mean() if len(lose_trades) > 0 else 0
        
        # 佣金统计
        total_commission = trades_df['commission'].sum() if not trades_df.empty else 0
        
        # 止损统计
        stop_loss_trades = trades_df[trades_df['action'] == 'STOP_LOSS']
        
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
            'stop_loss_count': len(stop_loss_trades),
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

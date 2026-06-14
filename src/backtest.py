"""
回测引擎 - ETF轮动策略回测 (v1.1 防御型交易规则版本)
支持：交易费率、止损、仓位控制、大盘择时、防御资产、动态止盈

版本定位：
  v1.1 = "防御型交易规则版本"
  核心内容：防御资产层（黄金/国债）+ 调仓日规则 + 动态止盈 + 冷静期
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json

from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, DEFENSE_ALLOCATION, BACKTEST_CONFIG
from strategy import StrategyEngine



class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, cfg=None):
        self.cfg = cfg or STRATEGY_CONFIG.copy()
        # v1.2: 自动合并市场状态配置（observer 模式，不改变交易逻辑）
        from config import MARKET_REGIME_CONFIG
        if not self.cfg.get('enabled', False):
            self.cfg.update(MARKET_REGIME_CONFIG)
        self.strategy = StrategyEngine(self.cfg)
        self.initial_capital = self.cfg.get('initial_capital', BACKTEST_CONFIG['initial_capital'])
    
    def run(self, market_df: pd.DataFrame, bench_df: pd.DataFrame) -> dict:
        """
        运行回测
        
        正确评分路径：
        1. 分离股票ETF和防御资产行情
        2. 逐ETF计算指标和评分（不含动量排名）
        3. 合并所有股票ETF的scores，按日期做momentum_20横截面排名
        4. 计算股票ETF的total_score
        5. 对防御资产单独计算简化评分（不依赖动量排名）
        6. 合并信号（含大盘择时）
        7. 执行回测（含防御模块、动态止盈）
        
        Parameters:
            market_df: 所有ETF的行情数据（含股票ETF+防御资产）
            bench_df: 基准（沪深300）行情数据
        
        Returns:
            dict with backtest results
        """
        import config as _cfg_module
        _stock_tickers = list(_cfg_module.ETF_UNIVERSE.keys())
        _fallback_tickers = list(getattr(_cfg_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
        _defense_tickers = list(_cfg_module.DEFENSE_UNIVERSE.keys())
        
        # 分离三类资产
        stock_df = market_df[market_df['ticker'].isin(_stock_tickers)].copy()
        fallback_df = market_df[market_df['ticker'].isin(_fallback_tickers)].copy()
        defense_df = market_df[market_df['ticker'].isin(_defense_tickers)].copy()
        
        # 步骤1-2：对行业ETF逐只计算指标和评分
        all_scores = []
        for ticker in stock_df['ticker'].unique():
            ticker_df = stock_df[stock_df['ticker'] == ticker].copy()
            if len(ticker_df) < 50:  # 数据不足跳过
                continue
            scored = self.strategy.calculate_total_score(ticker_df)
            all_scores.append(scored)
        
        if not all_scores:
            return {'error': '无有效行业ETF数据'}
        
        scores_df = pd.concat(all_scores, ignore_index=True)
        
        # 步骤3：行业ETF全池横截面动量排名
        scores_df = self.strategy.rank_all_momentum(scores_df)
        
        # 步骤4：计算行业ETF总评分
        scores_df = self.strategy.compute_total_score(scores_df)
        
        # 步骤5：对宽基补仓ETF单独计算简化评分（不参与横截面动量排名）
        fallback_scores = []
        for ticker in fallback_df['ticker'].unique():
            ticker_df = fallback_df[fallback_df['ticker'] == ticker].copy()
            if len(ticker_df) < 50:
                continue
            scored = self.strategy.calculate_fallback_equity_score(ticker_df)
            fallback_scores.append(scored)
        
        if fallback_scores:
            fallback_scores_df = pd.concat(fallback_scores, ignore_index=True)
            # 宽基已计算好所有子项，直接求和
            fallback_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
            fallback_scores_df['total_score'] = fallback_scores_df[fallback_cols].fillna(0).sum(axis=1)
            scores_df = pd.concat([scores_df, fallback_scores_df], ignore_index=True)
        
        # 步骤6：对防御资产单独计算简化评分
        defense_scores = []
        for ticker in defense_df['ticker'].unique():
            ticker_df = defense_df[defense_df['ticker'] == ticker].copy()
            if len(ticker_df) < 50:
                continue
            # 防御资产简化评分：只计算趋势指标
            scored = self.strategy.calculate_defense_score(ticker_df)
            defense_scores.append(scored)
        
        if defense_scores:
            defense_scores_df = pd.concat(defense_scores, ignore_index=True)
            # 防御资产不参与动量排名，直接合并
            scores_df = pd.concat([scores_df, defense_scores_df], ignore_index=True)
            # 修复：防御资产合并后需要补充 total_score
            # 防御资产已计算好所有子项，直接求和
            defense_cols = ['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']
            # 用 fillna(0) 确保缺失列不报错，但防御资产理论上都有这些列
            scores_df['total_score'] = scores_df['total_score'].fillna(
                scores_df[defense_cols].fillna(0).sum(axis=1)
            )
        
        # 步骤6：生成信号（含大盘择时合并）
        signals_df = self.strategy.generate_signals(scores_df, bench_df)
        
        # 步骤7：执行回测
        result = self._execute_backtest(signals_df, market_df, bench_df)
        
        # v1.2: 市场状态检测（observer 模式，不改变交易逻辑）
        if self.cfg.get('enabled', False) and self.cfg.get('mode', '') == 'observer' and 'error' not in result:
            from market_regime import MarketRegimeDetector
            detector = MarketRegimeDetector(self.cfg)
            regime_history = detector.detect_history(bench_df, stock_df)
            
            if not regime_history.empty and 'nav_df' in result and not result['nav_df'].empty:
                # 合并 regime 到 nav_df
                result['nav_df'] = result['nav_df'].merge(
                    regime_history[['date', 'regime_id', 'regime_name', 'confidence', 'reason']],
                    on='date', how='left'
                )
                result['regime_history'] = regime_history
                result['regime_summary'] = detector.get_state_summary(regime_history)
        
        return result
    
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
            day_prices = market_df[market_df['date'] == date].set_index('ticker')['open'].to_dict()
            day_close_prices = market_df[market_df['date'] == date].set_index('ticker')['close'].to_dict()
            
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
                
                # ===== 强制减仓：当 market_signal < 1.0 时，如果总仓位超过目标，卖出非防御持仓 =====
                import config as _cfg_module
                _defense_tickers_for_reduce = list(_cfg_module.DEFENSE_UNIVERSE.keys())
                
                # 重新计算当前净值和可用资金
                current_value_reduce = portfolio['cash'] + sum(
                    portfolio['positions'][t]['shares'] * day_prices.get(t, 0)
                    for t in portfolio['positions']
                )
                target_total_value_reduce = current_value_reduce * max_total_position
                current_positions_value = sum(
                    portfolio['positions'][t]['shares'] * day_prices.get(t, 0)
                    for t in portfolio['positions']
                )
                
                # 如果当前持仓市值超过目标仓位，需要减仓
                if current_positions_value > target_total_value_reduce and max_total_position < 1.0:
                    excess_value = current_positions_value - target_total_value_reduce
                    
                    # 优先卖出非防御持仓中评分最低的
                    non_defense_for_reduce = [
                        (t, portfolio['positions'][t]) 
                        for t in portfolio['positions']
                        if t not in _defense_tickers_for_reduce and t in day_prices
                    ]
                    
                    # 按当前评分排序（低分先卖）
                    if non_defense_for_reduce:
                        position_scores_reduce = []
                        for t, pos in non_defense_for_reduce:
                            t_signal = day_signals[day_signals['ticker'] == t]
                            if not t_signal.empty:
                                position_scores_reduce.append((t, t_signal['total_score'].iloc[0], pos))
                        
                        position_scores_reduce.sort(key=lambda x: x[1])
                        
                        for sell_ticker, _, pos in position_scores_reduce:
                            if excess_value <= 0:
                                break
                            
                            price = day_prices[sell_ticker]
                            shares = pos['shares']
                            
                            proceeds = shares * price
                            commission = calc_commission(proceeds)
                            net_proceeds = proceeds - commission
                            
                            portfolio['cash'] += net_proceeds
                            excess_value -= proceeds
                            
                            pnl = (price - pos['cost']) / pos['cost']
                            trade_records.append({
                                'date': date_str,
                                'ticker': sell_ticker,
                                'action': 'SELL',
                                'price': price,
                                'shares': shares,
                                'amount': proceeds,
                                'commission': commission,
                                'pnl_pct': pnl,
                                'reason': '大盘择时强制减仓'
                            })
                            
                            del portfolio['positions'][sell_ticker]
                
                # 2. 买入新标的（考虑仓位控制 + 冷却期 + 防御模块）
                current_holdings = len(portfolio['positions'])
                max_new = self.cfg.get('total_max_holdings', self.cfg['max_holdings']) - current_holdings
                
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
                    
                    # ========== 防御模块（v1.3）==========
                    # 当大盘择时信号低时，强制配置防御资产（黄金/国债）
                    import config as _config_module
                    _defense_tickers = list(_config_module.DEFENSE_UNIVERSE.keys())
                    _stock_tickers = list(_config_module.ETF_UNIVERSE.keys())
                    _fallback_tickers = list(getattr(_config_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
                    
                    # 动态防御比例计算
                    _defense_allocation_mode = getattr(_config_module, 'DEFENSE_ALLOCATION_MODE', 'step')
                    _defense_allocation_map = _config_module.DEFENSE_ALLOCATION
                    
                    if _defense_allocation_mode == 'linear':
                        # 线性插值：根据max_total_position在关键点之间插值
                        sorted_signals = sorted(_defense_allocation_map.keys())
                        _defense_allocation = 0.0
                        for i in range(len(sorted_signals) - 1):
                            s_low, s_high = sorted_signals[i], sorted_signals[i + 1]
                            if s_low <= max_total_position <= s_high:
                                a_low = _defense_allocation_map[s_low]
                                a_high = _defense_allocation_map[s_high]
                                if s_high == s_low:
                                    _defense_allocation = a_low
                                else:
                                    _defense_allocation = a_low + (a_high - a_low) * (max_total_position - s_low) / (s_high - s_low)
                                break
                        else:
                            # 超出范围，取边界值
                            if max_total_position < sorted_signals[0]:
                                _defense_allocation = _defense_allocation_map[sorted_signals[0]]
                            else:
                                _defense_allocation = _defense_allocation_map[sorted_signals[-1]]
                    else:
                        # 阶梯式（原始逻辑）
                        _defense_allocation = _defense_allocation_map.get(max_total_position, 0.0)
                    
                    # 波动率增强（可选）
                    _vol_enhance = getattr(_config_module, 'VOLATILITY_ENHANCEMENT', {})
                    if _vol_enhance.get('enabled', False):
                        # 计算近期市场波动率（基于基准）
                        vol_lookback = _vol_enhance.get('lookback', 20)
                        bench_recent = bench_df[bench_df['date'] <= date].tail(vol_lookback)
                        if len(bench_recent) >= 5:
                            bench_returns = bench_recent['close'].pct_change().dropna()
                            if len(bench_returns) > 0:
                                current_vol = bench_returns.std() * np.sqrt(252)  # 年化波动率
                                vol_low = _vol_enhance.get('threshold_low', 0.15)
                                vol_high = _vol_enhance.get('threshold_high', 0.30)
                                adj_low = _vol_enhance.get('adjustment_low', -0.05)
                                adj_high = _vol_enhance.get('adjustment_high', 0.10)
                                
                                if current_vol <= vol_low:
                                    vol_adjustment = adj_low
                                elif current_vol >= vol_high:
                                    vol_adjustment = adj_high
                                else:
                                    vol_adjustment = adj_low + (adj_high - adj_low) * (current_vol - vol_low) / (vol_high - vol_low)
                                
                                _defense_allocation += vol_adjustment
                                _defense_allocation = max(_vol_enhance.get('min_allocation', 0.0), 
                                                        min(_vol_enhance.get('max_allocation', 0.80), _defense_allocation))
                    
                    if _defense_allocation > 0 and self.cfg.get('defense_enabled', True):
                        # 从买入信号中过滤防御资产
                        defense_signals = buy_signals[buy_signals['ticker'].isin(_defense_tickers)]
                        
                        # DEBUG: 打印防御模块状态（生产环境设为False）
                        if False:  # 改为True可开启调试
                            print(f"DEBUG {date_str}: defense_tickers={_defense_tickers}, "
                                  f"allocation={_defense_allocation}, max_new={max_new}, "
                                  f"defense_signals={len(defense_signals)}, "
                                  f"positions={list(portfolio['positions'].keys())}")
                        
                        if not defense_signals.empty:
                            # 如果持仓已满，强制卖出评分最低的股票ETF，为防御资产腾仓位
                            if max_new <= 0 and len(portfolio['positions']) > 0:
                                # 找出评分最低的非防御持仓
                                non_defense_positions = {
                                    t: p for t, p in portfolio['positions'].items() 
                                    if t not in _defense_tickers and t in day_prices
                                }
                                if non_defense_positions:
                                    # 获取这些持仓的当前评分
                                    position_scores = []
                                    for t in non_defense_positions:
                                        t_signal = day_signals[day_signals['ticker'] == t]
                                        if not t_signal.empty:
                                            position_scores.append((t, t_signal['total_score'].iloc[0]))
                                    
                                    if position_scores:
                                        # 卖出评分最低的
                                        position_scores.sort(key=lambda x: x[1])
                                        sell_ticker = position_scores[0][0]
                                        
                                        price = day_prices[sell_ticker]
                                        pos = portfolio['positions'][sell_ticker]
                                        shares = pos['shares']
                                        
                                        proceeds = shares * price
                                        commission = calc_commission(proceeds)
                                        net_proceeds = proceeds - commission
                                        
                                        portfolio['cash'] += net_proceeds
                                        
                                        pnl = (price - pos['cost']) / pos['cost']
                                        trade_records.append({
                                            'date': date_str,
                                            'ticker': sell_ticker,
                                            'action': 'SELL',
                                            'price': price,
                                            'shares': shares,
                                            'amount': proceeds,
                                            'commission': commission,
                                            'pnl_pct': pnl,
                                            'reason': '为防御资产腾仓位'
                                        })
                                        
                                        del portfolio['positions'][sell_ticker]
                                        max_new += 1
                            
                            # 现在有仓位了，配置防御资产（循环配置所有达标的防御资产）
                            if max_new > 0:
                                # 防御资产目标金额 = 当前净值 × 防御配置比例 × 大盘择时仓位
                                defense_target_total = current_value * _defense_allocation * max_total_position
                                
                                # 计算当前防御资产持仓市值
                                current_defense_value = sum(
                                    portfolio['positions'][t]['shares'] * day_prices.get(t, 0)
                                    for t in portfolio['positions'] if t in _defense_tickers
                                )
                                
                                # 还需要配置的防御资产金额
                                defense_needed = max(0, defense_target_total - current_defense_value)
                                defense_needed = min(defense_needed, available_cash * 0.95)
                                
                                # 防御资产评分门槛更宽松
                                min_defense_score = self.cfg.get('min_total_score', 40) - 10
                                
                                # 循环配置所有达标的防御资产（按评分排序）
                                for _, defense_row in defense_signals.iterrows():
                                    if max_new <= 0 or defense_needed < 1000:
                                        break
                                    
                                    ticker = defense_row['ticker']
                                    
                                    if ticker not in day_prices or ticker in portfolio['positions']:
                                        continue
                                    
                                    if defense_row['total_score'] < min_defense_score:
                                        continue
                                    
                                    price = day_prices[ticker]
                                    
                                    # 单只防御资产目标 = 防御_needed / 剩余防御资产数，或按评分分配
                                    # 简化：每只防御资产均分目标金额
                                    defense_target_per_etf = defense_needed / min(max_new, len(defense_signals))
                                    defense_target_per_etf = min(defense_target_per_etf, available_cash * 0.95)
                                    
                                    shares = int(defense_target_per_etf / price)
                                    if shares >= 1:
                                        cost = shares * price
                                        commission = calc_commission(cost)
                                        total_cost = cost + commission
                                        
                                        if total_cost <= portfolio['cash'] and total_cost <= available_cash * 0.95:
                                            portfolio['cash'] -= total_cost
                                            available_cash -= total_cost
                                            defense_needed -= cost
                                            max_new -= 1
                                            
                                            portfolio['positions'][ticker] = {
                                                'shares': shares,
                                                'cost': price,
                                                'entry_date': date_str,
                                                'high_water': price
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
                                                'reason': f"防御配置({max_total_position:.0%}仓位): 评分{defense_row['total_score']:.1f}"
                                            })
                    
                    # ========== 买入行业ETF（第二层） ==========
                    if max_new > 0:
                        # 只买入行业ETF（不含宽基和防御）
                        stock_signals = buy_signals[buy_signals['ticker'].isin(_stock_tickers)]
                        current_stock_holdings = sum(1 for t in portfolio['positions'] if t in _stock_tickers)
                        stock_slots = min(max_new, self.cfg['max_holdings'] - current_stock_holdings)
                        n_buy = min(stock_slots, len(stock_signals))
                        
                        if n_buy > 0:
                            base_weight = min(self.cfg['max_position_per_etf'], 1.0 / n_buy)
                            
                            for _, row in stock_signals.head(n_buy).iterrows():
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
                                    
                                    max_new -= 1
                                    
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
                    
                    # ========== 买入宽基补仓ETF（第二层）==========
                    # 默认关闭：回测显示当前参数下宽基补仓为负贡献
                    # 未来可通过 cfg['fallback_equity_enabled'] = True 启用测试
                    if max_new > 0 and available_cash > 1000 and self.cfg.get('fallback_equity_enabled', False):
                        fallback_signals = buy_signals[buy_signals['ticker'].isin(_fallback_tickers)]
                        # 过滤掉已在持仓中的宽基
                        fallback_signals = fallback_signals[~fallback_signals['ticker'].isin(portfolio['positions'].keys())]
                        
                        current_fallback_holdings = sum(1 for t in portfolio['positions'] if t in _fallback_tickers)
                        fallback_slots = min(max_new, self.cfg.get('fallback_equity_max_holdings', 3) - current_fallback_holdings)
                        n_fallback_buy = min(fallback_slots, len(fallback_signals))
                        
                        if n_fallback_buy > 0:
                            base_weight = min(self.cfg['max_position_per_etf'], 1.0 / n_fallback_buy)
                            
                            for _, row in fallback_signals.head(n_fallback_buy).iterrows():
                                ticker = row['ticker']
                                
                                # 检查冷却期（实验性v1.2）
                                if ticker in cooling_list:
                                    days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                                    if days_since_stop < self.cfg.get('cooling_period', 0):
                                        continue
                                
                                if ticker in day_prices and ticker not in portfolio['positions']:
                                    price = day_prices[ticker]
                                    
                                    # 冷却期后重新买入需要更高评分（实验性v1.2）
                                    min_score = self.cfg.get('min_fallback_score', 25)
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
                                        'high_water': price
                                    }
                                    
                                    # 从冷却期列表中移除（已重新买入）
                                    if ticker in cooling_list:
                                        del cooling_list[ticker]
                                    
                                    max_new -= 1
                                    
                                    trade_records.append({
                                        'date': date_str,
                                        'ticker': ticker,
                                        'action': 'BUY',
                                        'price': price,
                                        'shares': shares,
                                        'amount': cost,
                                        'commission': commission,
                                        'pnl_pct': 0,
                                        'reason': f"宽基补仓(评分{row['total_score']:.1f})"
                                    })
                    
                    # ========== 防御资产填充（第三层，优先级最低）==========
                    # 当行业ETF和宽基ETF都买不满时，用防御资产填充
                    # 但有上限限制：牛市最多30%，熊市/弱市最多50%
                    if max_new > 0 and self.cfg.get('defense_enabled', True) and available_cash > 1000:
                        defense_signals = buy_signals[buy_signals['ticker'].isin(_defense_tickers)]
                        # 过滤掉已在持仓中的防御资产
                        defense_signals = defense_signals[~defense_signals['ticker'].isin(portfolio['positions'].keys())]
                        
                        if not defense_signals.empty:
                            # 防御资产更宽松的评分门槛
                            min_defense_score = self.cfg.get('min_total_score', 40) - 15
                            
                            # 计算当前防御资产持仓比例
                            current_defense_value = sum(
                                portfolio['positions'][t]['shares'] * day_close_prices.get(t, 0)
                                for t in portfolio['positions'] if t in _defense_tickers
                            )
                            # 根据当前仓位上限确定防御资产填充上限
                            if max_total_position >= 1.0:
                                # 牛市（满仓）：防御资产最多30%
                                defense_fill_max = self.cfg.get('defense_fill_max_ratio_bull', 0.30)
                            else:
                                # 弱市/熊市：防御资产最多50%
                                defense_fill_max = self.cfg.get('defense_fill_max_ratio_bear', 0.50)
                            
                            # 计算本次可填充的最大防御资产金额
                            # 目标：防御资产总市值 ≤ current_value * defense_fill_max
                            current_value = portfolio['cash'] + sum(
                                portfolio['positions'][t]['shares'] * day_close_prices.get(t, 0)
                                for t in portfolio['positions']
                            )
                            max_defense_target = current_value * defense_fill_max
                            defense_fill_allowance = max(0, max_defense_target - current_defense_value)
                            
                            # 填充目标 = min(可用资金, 填充上限)
                            fill_target = min(available_cash * 0.95, defense_fill_allowance)
                            
                            # 使用 defense_max_holdings 控制数量
                            current_defense_holdings = sum(1 for t in portfolio['positions'] if t in _defense_tickers)
                            defense_slots = min(max_new, self.cfg.get('defense_max_holdings', 2) - current_defense_holdings)
                            n_defense_buy = min(defense_slots, len(defense_signals))
                            
                            for _, defense_row in defense_signals.iterrows():
                                if defense_slots <= 0 or max_new <= 0 or fill_target < 1000:
                                    break
                                
                                ticker = defense_row['ticker']
                                if ticker not in day_prices or ticker in portfolio['positions']:
                                    continue
                                
                                if defense_row['total_score'] < min_defense_score:
                                    continue
                                
                                price = day_prices[ticker]
                                
                                # 目标金额 = 剩余可用资金 / 剩余防御资产数
                                target = fill_target / min(defense_slots, len(defense_signals))
                                target = min(target, available_cash * 0.95)
                                
                                shares = int(target / price)
                                if shares < 1:
                                    continue
                                
                                cost = shares * price
                                commission = calc_commission(cost)
                                total_cost = cost + commission
                                
                                if total_cost > portfolio['cash'] or total_cost > available_cash * 0.95:
                                    continue
                                
                                portfolio['cash'] -= total_cost
                                available_cash -= total_cost
                                fill_target -= cost
                                defense_slots -= 1
                                max_new -= 1
                                
                                portfolio['positions'][ticker] = {
                                    'shares': shares,
                                    'cost': price,
                                    'entry_date': date_str,
                                    'high_water': price
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
                                    'reason': f"防御填充(评分{defense_row['total_score']:.1f})"
                                })
            
            # ========== 计算当日净值（用收盘价计算持仓市值）==========
            positions_value = 0
            for ticker, pos in portfolio['positions'].items():
                if ticker in day_close_prices:
                    positions_value += pos['shares'] * day_close_prices[ticker]
            
            total_value = portfolio['cash'] + positions_value
            
            # 获取当日基准价格
            bench_price = None
            if not bench_df[bench_df['date'] == date].empty:
                bench_price = bench_df[bench_df['date'] == date]['close'].iloc[0]
            
            # Calculate position allocations（用收盘价）
            positions_pct = {}
            if total_value > 0:
                for t, p in portfolio['positions'].items():
                    if t in day_close_prices:
                        positions_pct[t] = (p['shares'] * day_close_prices[t]) / total_value
            
            nav_records.append({
                'date': date_str,
                'nav': total_value,
                'cash': portfolio['cash'],
                'positions_value': positions_value,
                'num_positions': len(portfolio['positions']),
                'bench_price': bench_price,
                'max_total_position': max_total_position,
                'positions_pct': positions_pct,
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

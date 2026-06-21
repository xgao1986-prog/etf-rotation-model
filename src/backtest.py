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

from config import STRATEGY_CONFIG, ETF_UNIVERSE, CONCEPT_UNIVERSE, DEFENSE_UNIVERSE, DEFENSE_ALLOCATION, BACKTEST_CONFIG, CORE_UNIVERSE, CORRELATION_THRESHOLD, FALLBACK_EQUITY_UNIVERSE
from strategy import StrategyEngine
from rebalance_planner import plan_rebalance_v2_5



class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, cfg=None, s1_mode=False, slippage_bps=0):
        self.cfg = cfg or STRATEGY_CONFIG.copy()
        # v1.2: 自动合并市场状态配置（observer 模式，不改变交易逻辑）
        # 仅当用户未显式设置 enabled 时，才注入默认 MARKET_REGIME_CONFIG
        # 用户显式设置 enabled=False 时可以关闭 v1.2 observer
        from config import MARKET_REGIME_CONFIG
        if 'enabled' not in self.cfg:
            self.cfg.update(MARKET_REGIME_CONFIG)
        self.strategy = StrategyEngine(self.cfg, s1_mode=s1_mode)
        self.initial_capital = self.cfg.get('initial_capital', BACKTEST_CONFIG['initial_capital'])
        self.slippage_bps = slippage_bps  # 实验性：滑点（基点），默认0不影响现有行为
    
    def run(self, market_df: pd.DataFrame, bench_df: pd.DataFrame, universe_builder=None, eval_date=None, performance_start=None, early_exit_days=None, as_of_date=None) -> dict:
        """
        运行回测
        
        v5 修正：统一截断到全标的共同截止日
        v6 修正：支持 performance_start，用于样本外/滚动窗口预热
        v6.1：支持 early_exit_days，用于测试3日失败退出规则
        v6.2：支持 as_of_date，显式指定回测截止日期
        """
        import config as _cfg_module
        _core_tickers = list(getattr(_cfg_module, 'CORE_UNIVERSE', {}).keys())
        _fallback_tickers = list(getattr(_cfg_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
        _defense_tickers = list(_cfg_module.DEFENSE_UNIVERSE.keys())
        
        # ========== v6.2: 回测截止日期控制 ==========
        if as_of_date is not None:
            cutoff = pd.Timestamp(as_of_date)
        else:
            cutoff = market_df['date'].max()
        market_df = market_df[market_df['date'] <= cutoff].copy()
        bench_df = bench_df[bench_df['date'] <= cutoff].copy()
        
        # ========== v6.2: 日期验证 ==========
        # 使用实际传入的行情池，而非硬编码的全池（B0.3只传入18只，不应显示38只）
        all_tickers = set(market_df['ticker'].unique())
        market_max_date = market_df['date'].max()
        bench_max_date = bench_df['date'].max()
        
        # 截止日有数据的ETF
        cutoff_date = pd.Timestamp(as_of_date) if as_of_date else market_max_date
        last_day_data = market_df[market_df['date'] == cutoff_date]
        present_tickers = set(last_day_data['ticker'].unique()) if not last_day_data.empty else set()
        missing_tickers = sorted(all_tickers - present_tickers)
        
        print("=" * 70)
        print("回测日期验证")
        print("=" * 70)
        print(f"  请求截止日期:        {cutoff_date.strftime('%Y-%m-%d')}")
        print(f"  ETF数据最大日期:     {market_max_date.strftime('%Y-%m-%d')}")
        print(f"  基准数据最大日期:    {bench_max_date.strftime('%Y-%m-%d')}")
        print(f"  参与回测ETF数量:     {len(all_tickers)} (实际传入行情池)")
        print(f"  截止日有数据ETF:     {len(present_tickers)}")
        print(f"  数据缺失ETF:         {len(missing_tickers)} 只")
        if missing_tickers:
            for t in missing_tickers:
                t_last = market_df[market_df['ticker'] == t]['date'].max()
                print(f"    - {t}: 最后日期 {t_last.strftime('%Y-%m-%d') if pd.notna(t_last) else 'N/A'}")
        
        if as_of_date and market_max_date < pd.Timestamp(as_of_date):
            raise ValueError(f"ERROR: 请求截止日 {as_of_date} 晚于数据最大日期 {market_max_date}")
        if as_of_date and bench_max_date < pd.Timestamp(as_of_date):
            raise ValueError(f"ERROR: 请求截止日 {as_of_date} 晚于基准数据最大日期 {bench_max_date}")
        print("=" * 70)
        
        # ========== v1.2.1: 动态池评估（Walk-forward）==========
        # 如果提供了 universe_builder 和 eval_date，则使用动态池评估
        # 否则回退到使用 config 中的静态池
        core_tickers = set(_core_tickers)
        fallback_tickers = set(_fallback_tickers)
        defense_tickers = set(_defense_tickers)
        
        if universe_builder and eval_date:
            # 动态池评估：在 eval_date 时点，使用当时已知的数据
            all_equity_tickers = list(core_tickers | fallback_tickers)
            eval_result = universe_builder.evaluate_at_date(all_equity_tickers, eval_date)
            pools = eval_result['pools']
            
            # 过滤：只使用 core + enhanced + fallback 池中的ETF
            # watch 和 excluded 池不参与交易（流动性不足或数据太少）
            allowed_equity = set(pools.get('core', [])) | set(pools.get('enhanced', [])) | set(pools.get('fallback', []))
            
            # 记录池状态用于日志
            print(f"\n[Pool Status at {eval_date}] Core={len(pools.get('core',[]))}, Enhanced={len(pools.get('enhanced',[]))}, Watch={len(pools.get('watch',[]))}, Fallback={len(pools.get('fallback',[]))}, Excluded={len(pools.get('excluded',[]))}")
            
            # 更新 ticker 集合用于过滤行情数据
            core_tickers = core_tickers & allowed_equity
            fallback_tickers = fallback_tickers & allowed_equity
        
        # 分离三类资产
        core_df = market_df[market_df['ticker'].isin(core_tickers)].copy()
        fallback_df = market_df[market_df['ticker'].isin(fallback_tickers)].copy()
        defense_df = market_df[market_df['ticker'].isin(defense_tickers)].copy()
        
        # ========== v1.2.1: 事前硬去重（Pool Pre-filter）==========
        # 在评分前自动剔除高度冗余ETF，避免"同一敞口两种包装"的问题
        # 规则：滚动60日相关性均值 >= 0.97 视为冗余对
        # 保留：历史数据天数更多的（上市更长的）
        # v6: 样本外/滚动窗口时，只使用绩效起点之前的数据计算相关性，禁止用完整区间均值
        HARD_REDUNDANCY_THRESHOLD = 0.97
        
        _excluded_tickers = set()
        _exclusion_reasons = {}
        
        # v6: 硬去重判断数据集 = performance_start 之前的数据（样本外），或完整数据（全样本/样本内）
        hard_judgment_date = None
        if performance_start is not None:
            hard_df = core_df[core_df['date'] < pd.to_datetime(performance_start)].copy()
            hard_judgment_date = str(pd.to_datetime(performance_start).strftime('%Y-%m-%d'))
        else:
            hard_df = core_df.copy()
            hard_judgment_date = 'full_sample'
        
        if len(hard_df['ticker'].unique()) > 1:
            # 计算滚动60日相关性
            pivot = hard_df.pivot_table(index='date', columns='ticker', values='close')
            returns = pivot.pct_change(fill_method=None)
            min_valid = int(60 * 0.67)
            
            # 计算每对ETF的滚动60日相关性均值
            tickers = returns.columns.tolist()
            for i, t1 in enumerate(tickers):
                for t2 in tickers[i+1:]:
                    if t1 in _excluded_tickers or t2 in _excluded_tickers:
                        continue
                    
                    # 计算滚动相关性
                    rolling_corr = returns[t1].rolling(60, min_periods=min_valid).corr(returns[t2])
                    valid_corr = rolling_corr.dropna()
                    
                    if len(valid_corr) > 0:
                        mean_corr = valid_corr.mean()
                        if mean_corr >= HARD_REDUNDANCY_THRESHOLD:
                            # 冗余对：保留数据天数更多的（在判断数据集中比较）
                            days1 = len(hard_df[hard_df['ticker'] == t1])
                            days2 = len(hard_df[hard_df['ticker'] == t2])
                            
                            if days1 >= days2:
                                exclude = t2
                                keep = t1
                            else:
                                exclude = t1
                                keep = t2
                            
                            _excluded_tickers.add(exclude)
                            _exclusion_reasons[exclude] = (
                                f"Redundant: corr={mean_corr:.4f} with {keep} >= {HARD_REDUNDANCY_THRESHOLD}, "
                                f"keep {keep}({days1}d) vs exclude {exclude}({days2}d), "
                                f"judgment_date={hard_judgment_date}"
                            )
        
        # 从核心池剔除冗余ETF
        if _excluded_tickers:
            # 避免编码问题：使用英文日志
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[Pool Pre-filter] Excluded: {_excluded_tickers}")
            for t, reason in _exclusion_reasons.items():
                logger.info(f"  {t}: {reason}")
            core_df = core_df[~core_df['ticker'].isin(_excluded_tickers)].copy()
        
        # 步骤1-2：对核心池（行业+概念）逐只计算指标和评分
        # v6: 数据不足51天的ETF不参与评分（MA50经shift(1)后第51个交易日才有效）
        all_scores = []
        for ticker in core_df['ticker'].unique():
            ticker_df = core_df[core_df['ticker'] == ticker].copy()
            if len(ticker_df) < 51:  # 不足51个交易日，无法产生完整指标
                continue
            scored = self.strategy.calculate_total_score(ticker_df)
            all_scores.append(scored)
        
        if not all_scores:
            return {'error': '无有效核心池数据'}
        
        scores_df = pd.concat(all_scores, ignore_index=True)
        
        # 步骤3：核心池全池横截面动量排名（32只统一排名）
        scores_df = self.strategy.rank_all_momentum(scores_df)
        
        # 步骤4：计算核心池总评分
        # v6.1: 支持因子消融测试，从cfg中读取exclude_factor
        _exclude_factor = self.cfg.get('exclude_factor', None)
        scores_df = self.strategy.compute_total_score(scores_df, exclude_factor=_exclude_factor)
        
        # 步骤4.5：软惩罚（Soft Penalty）
        # 对于与池中其他ETF相关性在0.85-0.97之间的，降低total_score
        # 目的：解决"不同主题但高度相关"的冗余问题
        # v6: 样本外/滚动窗口时，只使用绩效起点之前的数据计算相关性，禁止用完整区间均值
        SOFT_PENALTY_MIN = 0.85
        SOFT_PENALTY_MAX = 0.97
        SOFT_PENALTY_MAX_REDUCTION = 0.15  # 最多减少15%（原为30%）
        
        # v6: 软惩罚判断数据集 = performance_start 之前的数据（样本外），或完整数据（全样本/样本内）
        soft_judgment_date = None
        if performance_start is not None:
            soft_df = core_df[core_df['date'] < pd.to_datetime(performance_start)].copy()
            soft_judgment_date = str(pd.to_datetime(performance_start).strftime('%Y-%m-%d'))
        else:
            soft_df = core_df.copy()
            soft_judgment_date = 'full_sample'
        
        if len(soft_df['ticker'].unique()) > 1:
            # 计算全池滚动相关性
            pivot_sp = soft_df.pivot_table(index='date', columns='ticker', values='close')
            returns_sp = pivot_sp.pct_change(fill_method=None)
            min_valid_sp = int(60 * 0.67)
            
            tickers_sp = returns_sp.columns.tolist()
            max_corr = {}
            for t in tickers_sp:
                max_corr[t] = 0.0
                for other in tickers_sp:
                    if other == t:
                        continue
                    rolling_corr = returns_sp[t].rolling(60, min_periods=min_valid_sp).corr(returns_sp[other])
                    valid = rolling_corr.dropna()
                    if len(valid) > 0:
                        mean_corr = valid.mean()
                        if mean_corr > max_corr[t]:
                            max_corr[t] = mean_corr
            
            # 应用penalty
            scores_df['soft_penalty'] = 0.0
            scores_df['max_corr_peer'] = 0.0
            scores_df['soft_penalty_judgment_date'] = soft_judgment_date
            for t, corr in max_corr.items():
                if SOFT_PENALTY_MIN <= corr < SOFT_PENALTY_MAX:
                    reduction = SOFT_PENALTY_MAX_REDUCTION * (corr - SOFT_PENALTY_MIN) / (SOFT_PENALTY_MAX - SOFT_PENALTY_MIN)
                    mask = scores_df['ticker'] == t
                    scores_df.loc[mask, 'total_score'] = scores_df.loc[mask, 'total_score'] * (1 - reduction)
                    scores_df.loc[mask, 'soft_penalty'] = reduction
                    scores_df.loc[mask, 'max_corr_peer'] = corr
        
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
        
        # 预计算核心池的相关性矩阵（用于相关性去重）
        # 使用最近60日收盘价计算滚动相关性
        _corr_threshold = getattr(_cfg_module, 'CORRELATION_THRESHOLD', 0.70)
        corr_matrix = self._compute_correlation_matrix(core_df, window=60)
        
        # 步骤7：生成信号（含大盘择时合并）
        signals_df = self.strategy.generate_signals(scores_df, bench_df)
        
        # v6: 计算统一比较起点
        # 找到首个满足最低成熟行业ETF数量的日期（默认=持仓上限）
        # 只统计 core ETF（行业ETF），且必须同时满足：history_count >= 51 AND momentum_valid
        # 防御资产和后备资产不计入统一起点
        mature_count = signals_df.groupby('date').apply(
            lambda x: (x['ticker'].isin(_core_tickers) & (x['history_count'] >= 51) & x['momentum_valid']).sum()
        ).reset_index()
        mature_count.columns = ['date', 'mature_count']
        
        min_mature_count = self.cfg.get('min_mature_count', self.cfg.get('max_holdings', 5))
        valid_dates = mature_count[mature_count['mature_count'] >= min_mature_count]['date']
        
        if len(valid_dates) > 0:
            unified_start = valid_dates.min()
        else:
            unified_start = signals_df['date'].min()  # 回退到最早信号日期
        
        # 步骤8：执行回测（含相关性去重 + 防御模块 + 动态止盈）
        # v1.2.1: 传递 enhanced_tickers 用于区分仓位限制
        if universe_builder and eval_date:
            _enhanced_tickers = set(pools.get('enhanced', []))
        else:
            _enhanced_tickers = set()
        result = self._execute_backtest(
            signals_df, market_df, bench_df, corr_matrix, 
            _corr_threshold, _excluded_tickers, _enhanced_tickers,
            unified_start=unified_start, min_mature_count=min_mature_count,
            performance_start=performance_start,
            early_exit_days=early_exit_days
        )
        
        # 记录排除信息（供审计）
        result['excluded_tickers'] = list(_excluded_tickers)
        result['exclusion_reasons'] = _exclusion_reasons
        
        # v1.2: 市场状态检测（observer 模式，不改变交易逻辑）
        if self.cfg.get('enabled', False) and self.cfg.get('mode', '') == 'observer' and 'error' not in result:
            from market_regime import MarketRegimeDetector
            detector = MarketRegimeDetector(self.cfg)
            regime_history = detector.detect_history(bench_df, core_df)
            
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
    
    def _rebalance_v2(self, portfolio, day_signals, day_prices, effective_close_prices,
                      last_valid_close, date, date_str, buy_signals, trade_records,
                      cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                      _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                      buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                      min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                      calc_commission, slippage=0.0):
        """
        v2.5 纯函数调仓集成：顺序独立、总仓位受控、缺价不强制归零
        替代旧调仓大段逻辑（防御模块 + 核心池 + 备选池 + 防御填充）
        """
        # 1. 提取当前持仓
        current_positions = {t: p['shares'] for t, p in portfolio['positions'].items()}
        
        # 2. 准备候选列表（考虑冷却期和排名缓冲）
        raw_industry_candidates = []
        raw_defense_candidates = []
        
        for _, row in buy_signals.iterrows():
            ticker = row['ticker']
            score = row['total_score']
            
            # 冷却期检查
            if ticker in cooling_list:
                days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                if days_since_stop < self.cfg.get('cooling_period', 0):
                    continue
            
            # 评分检查（含冷却期后加分）
            min_score = self.cfg['min_total_score']
            if ticker in cooling_list:
                days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                if days_since_stop >= self.cfg.get('cooling_period', 0):
                    min_score += self.cfg.get('cooling_score_boost', 0)
            
            if score < min_score:
                continue
            
            if ticker in _defense_tickers:
                raw_defense_candidates.append((ticker, score))
            elif ticker in _core_tickers or ticker in _fallback_tickers:
                raw_industry_candidates.append((ticker, score))
        
        # 排名缓冲：只取前 buy_rank_n 个行业候选
        if rank_buffer_enabled and buy_rank_n is not None:
            raw_industry_candidates = raw_industry_candidates[:buy_rank_n]
        
        # 防御资产评分门槛更宽松
        min_defense_score = self.cfg.get('min_total_score', 40) - 10
        raw_defense_candidates = [(t, s) for t, s in raw_defense_candidates if s >= min_defense_score]
        
        # 3. 计算 NAV（使用与纯函数相同的估值逻辑：有当日价格用当日，无当日用last_prices）
        nav = portfolio['cash']
        for t, p in portfolio['positions'].items():
            price = day_prices.get(t, 0)
            if price > 0:
                nav += p['shares'] * price
            elif last_valid_close and t in last_valid_close and last_valid_close[t] > 0:
                nav += p['shares'] * last_valid_close[t]
            # 两者皆无的持仓不计入NAV（但会触发plan_rebalance_v2_5的unpriced_positions错误）
        
        # 4. 准备 prices 和 last_prices
        prices = dict(day_prices)
        last_prices = dict(last_valid_close) if last_valid_close else None
        
        # 5. 调用纯函数
        orders, state = plan_rebalance_v2_5(
            nav=nav,
            cash=portfolio['cash'],
            current_positions=current_positions,
            industry_candidates=raw_industry_candidates,
            defense_candidates=raw_defense_candidates,
            prices=prices,
            industry_tickers=set(_core_tickers) | set(_fallback_tickers),
            defense_tickers=set(_defense_tickers),
            last_prices=last_prices,
            max_industry_holdings=self.cfg['max_holdings'],
            max_defense_holdings=self.cfg.get('defense_max_holdings', 2),
            max_total_holdings=self.cfg.get('total_max_holdings', self.cfg['max_holdings']),
            max_position_per_etf=self.cfg['max_position_per_etf'],
            max_total_position=max_total_position,
            commission_rate=self.cfg['commission_rate'],
            min_commission=self.cfg['min_commission'],
            lot_size=100,
        )
        
        # 6. 执行订单（先卖出，后买入）
        # 先执行所有卖出
        for order in orders:
            if order['action'] != 'SELL':
                continue
            
            ticker = order['ticker']
            if ticker not in portfolio['positions']:
                continue
            
            pos = portfolio['positions'][ticker]
            shares = order['shares']
            # 应用滑点：卖出成交价下调
            price = order['price'] * (1 - slippage)
            amount = shares * price
            commission = calc_commission(amount)
            net_proceeds = amount - commission
            
            portfolio['cash'] += net_proceeds
            
            pnl = (price - pos['cost']) / pos['cost'] if pos['cost'] > 0 else 0
            
            trade_records.append({
                'date': date_str,
                'ticker': ticker,
                'action': 'SELL',
                'price': price,
                'shares': shares,
                'amount': amount,
                'commission': commission,
                'pnl_pct': pnl,
                'reason': order['reason'],
            })
            
            if shares >= pos['shares']:
                del portfolio['positions'][ticker]
            else:
                pos['shares'] -= shares
        
        # 再执行所有买入
        for order in orders:
            if order['action'] != 'BUY':
                continue
            
            ticker = order['ticker']
            shares = order['shares']
            # 应用滑点：买入成交价上调
            price = order['price'] * (1 + slippage)
            amount = shares * price
            commission = calc_commission(amount)
            total_cost = amount + commission
            
            # 相关性去重检查
            if corr_matrix and date in corr_matrix and ticker in corr_matrix[date]:
                skip = False
                for selected_ticker in portfolio['positions']:
                    if selected_ticker in corr_matrix[date][ticker]:
                        corr = corr_matrix[date][ticker][selected_ticker]
                        if corr > corr_threshold:
                            skip = True
                            break
                if skip:
                    continue
            
            # 同类分组检查
            if same_group_max > 0 and ticker in etf_group_map:
                ticker_group = etf_group_map[ticker]
                group_holdings = [t for t in portfolio['positions'] if t in etf_group_map and etf_group_map[t] == ticker_group]
                if len(group_holdings) >= same_group_max:
                    continue
            
            # 现金检查（纯函数已确保，但保险）
            if total_cost > portfolio['cash']:
                continue
            
            portfolio['cash'] -= total_cost
            
            # 获取 ATR
            atr = 0
            ticker_signals = day_signals[day_signals['ticker'] == ticker]
            if not ticker_signals.empty and 'atr_14' in ticker_signals.columns:
                atr = ticker_signals['atr_14'].iloc[0]
            
            if ticker in portfolio['positions']:
                # 加仓（纯函数不应买入已有持仓，但保险）
                old_pos = portfolio['positions'][ticker]
                old_shares = old_pos['shares']
                old_cost = old_pos['cost']
                new_shares = old_shares + shares
                new_cost = (old_shares * old_cost + shares * price) / new_shares if new_shares > 0 else price
                old_pos['shares'] = new_shares
                old_pos['cost'] = new_cost
                old_pos['high_water'] = max(old_pos['high_water'], price)
            else:
                portfolio['positions'][ticker] = {
                    'shares': shares,
                    'cost': price,
                    'entry_date': date_str,
                    'high_water': price,
                    'days_held': 0,
                    'atr_at_entry': atr,
                }
            
            trade_records.append({
                'date': date_str,
                'ticker': ticker,
                'action': 'BUY',
                'price': price,
                'shares': shares,
                'amount': amount,
                'commission': commission,
                'pnl_pct': 0,
                'reason': order['reason'],
            })
            
            # 从冷却期列表中移除
            if ticker in cooling_list:
                del cooling_list[ticker]
    
    def _compute_correlation_matrix(self, core_df: pd.DataFrame, window: int = 60) -> dict:
        """
        预计算核心池的滚动相关性矩阵
        
        使用 pct_change(fill_method=None) 避免前向填充污染。
        要求每对ETF在窗口内有至少 window*0.67 个有效共同交易日。
        
        返回：{date: {ticker1: {ticker2: corr, ...}, ...}, ...}
        """
        # 将数据转为宽格式（日期为行，ticker为列）
        pivot = core_df.pivot_table(index='date', columns='ticker', values='close')
        
        # 计算每日收益率，不使用前向填充
        returns = pivot.pct_change(fill_method=None)
        
        min_valid_pairs = int(window * 0.67)  # 至少67%的共同交易日
        
        # 使用 rolling.corr 批量计算，min_periods 确保有效数据量
        # rolling.corr 返回 MultiIndex: (date, ticker1, ticker2)
        rolling_corr = returns.rolling(window=window, min_periods=min_valid_pairs).corr()
        
        corr_history = {}
        dates = returns.index.tolist()
        
        for i, date in enumerate(dates):
            if i < window:
                continue
            
            # 获取该日期的相关性矩阵切片
            if date in rolling_corr.index.get_level_values(0):
                day_corr = rolling_corr.loc[date]
                
                # 转为字典格式，只保留非NaN且ticker不同的项
                corr_dict = {}
                for t1 in day_corr.columns:
                    corr_dict[t1] = {}
                    for t2 in day_corr.columns:
                        if t1 != t2:
                            val = day_corr.loc[t1, t2]
                            if pd.notna(val):
                                corr_dict[t1][t2] = val
                
                corr_history[date] = corr_dict
        
        return corr_history
    
    def _execute_backtest(self, signals_df, market_df, bench_df, corr_matrix=None, corr_threshold=0.70, excluded_tickers=None, enhanced_tickers=None, unified_start=None, min_mature_count=5, performance_start=None, early_exit_days=None) -> dict:
        """执行回测逻辑（含相关性去重）
        
        v6: 支持 performance_start，用于样本外/滚动窗口预热。
            指标计算在完整数据上进行，但交易和NAV记录从 performance_start 开始。
            performance_start 当天重置 portfolio 为初始资金。
        v6.1: 支持 early_exit_days，买入后N个交易日仍低于成本则强制退出。
        """
        
        excluded_tickers = set(excluded_tickers or [])
        enhanced_tickers = set(enhanced_tickers or [])
        
        # 获取所有交易日
        dates = sorted(signals_df['date'].unique())
        
        # ========== v6: 预热期与统一比较起点 ==========
        # unified_start 由 run() 传入：首个满足最低成熟ETF数量的日期
        # 只有 history_count >= 51 的ETF才计入成熟数量
        etf_data_start = {}
        for ticker in market_df['ticker'].unique():
            first_date = market_df[market_df['ticker'] == ticker]['date'].min()
            etf_data_start[ticker] = first_date
        
        earliest_data_start = min(etf_data_start.values())
        
        if unified_start is not None:
            warmup_end = pd.to_datetime(unified_start)
        else:
            # 回退：计算最早ETF的第51个交易日（索引50）
            earliest_ticker = [t for t, d in etf_data_start.items() if d == earliest_data_start][0]
            earliest_df = market_df[market_df['ticker'] == earliest_ticker].sort_values('date').reset_index(drop=True)
            if len(earliest_df) >= 51:
                warmup_end = earliest_df.iloc[50]['date']  # 第51个交易日
            else:
                warmup_end = earliest_data_start
        
        # 关键日期记录
        key_dates = {
            'earliest_data_start': str(earliest_data_start),
            'warmup_end': str(warmup_end),
            'unified_start': str(unified_start) if unified_start else str(warmup_end),
            'min_mature_count': min_mature_count,
        }
        
        # 预热期信息
        warmup_info = {
            'etf_data_start': {t: str(d) for t, d in etf_data_start.items()},
            'earliest_data_start': str(earliest_data_start),
            'warmup_end': str(warmup_end),
            'warmup_days': 51,  # 第51个交易日才是第一个完整指标集
            'unified_start': str(unified_start) if unified_start else str(warmup_end),
            'min_mature_count': min_mature_count,
        }
        
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
        rebalance_count = 0
        rebalance_dates = []
        
        # v6: 关键日期跟踪
        key_dates = {
            'first_mature_date': None,      # 首个 history_count >= 51 的日期
            'first_ranking_date': None,     # 首个有横截面排名的日期
            'first_signal_date': None,      # 首个有有效 BUY 信号的日期
            'first_buy_date': None,         # 首笔买入的日期
            'unified_start': str(warmup_end),  # 统一比较起点
            'performance_start': str(performance_start) if performance_start else None,  # 样本外/滚动窗口绩效起点
        }
        
        # v5: 缺失价格修复——维护最近有效收盘价，禁止持仓市值归零
        last_valid_close = {}  # ticker -> last valid close price
        missing_price_log = []  # list of missing price events
        missing_price_counter = {}  # ticker -> consecutive missing days
        
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
            
            # v5: 更新最近有效收盘价
            for ticker, price in day_close_prices.items():
                if pd.notna(price) and price > 0:
                    last_valid_close[ticker] = price
                    missing_price_counter[ticker] = 0
            
            # v5: 构建有效收盘价（缺失的用 last_valid_close，从未有过的为0）
            effective_close_prices = {}
            for ticker in day_close_prices:
                if pd.notna(day_close_prices[ticker]) and day_close_prices[ticker] > 0:
                    effective_close_prices[ticker] = day_close_prices[ticker]
                elif ticker in last_valid_close:
                    effective_close_prices[ticker] = last_valid_close[ticker]
                else:
                    effective_close_prices[ticker] = 0
            # 补充：对于不在 day_close_prices 中但持仓中的ETF，也使用 last_valid_close
            for ticker in portfolio['positions']:
                if ticker not in effective_close_prices and ticker in last_valid_close:
                    effective_close_prices[ticker] = last_valid_close[ticker]
            
            # 获取大盘择时信号
            max_total_position = 1.0
            if not day_signals.empty and 'market_signal' in day_signals.columns:
                max_total_position = day_signals['market_signal'].iloc[0]
            
            # 导入 core tickers（用于判断成熟行业ETF）
            import config as _cfg_module
            _core_tickers = list(getattr(_cfg_module, 'CORE_UNIVERSE', {}).keys())
            _fallback_tickers = list(getattr(_cfg_module, 'FALLBACK_EQUITY_UNIVERSE', {}).keys())
            _defense_tickers = list(_cfg_module.DEFENSE_UNIVERSE.keys())
            
            # v6: 记录关键日期
            # 首个 history_count >= 51 的日期（任意ETF）
            if not day_signals.empty and 'history_count' in day_signals.columns:
                mature_today = day_signals[day_signals['history_count'] >= 51]
                if not mature_today.empty and key_dates['first_mature_date'] is None:
                    key_dates['first_mature_date'] = date_str
            
            # 首个有意义的横截面排名日期：至少 min_mature_count 只成熟行业ETF共同参与排名
            # 定义：core ETF AND history_count >= 51 AND momentum_valid
            if not day_signals.empty and 'momentum_valid' in day_signals.columns:
                core_mature = day_signals[
                    day_signals['ticker'].isin(_core_tickers) & 
                    (day_signals['momentum_valid'] == True) & 
                    (day_signals['history_count'] >= 51)
                ]
                if len(core_mature) >= min_mature_count and key_dates['first_ranking_date'] is None:
                    key_dates['first_ranking_date'] = date_str
            
            # ========== v6: 预热期与绩效起点处理 ==========
            # 在预热期结束之前，不执行任何交易（不买入、不卖出、不止损）
            # NAV保持初始资金，只记录基准价格
            is_warmup = pd.to_datetime(date) < pd.to_datetime(warmup_end)
            # 性能起点：用于样本外/滚动窗口，pre-performance 期间不交易不记录NAV
            is_pre_performance = performance_start is not None and pd.to_datetime(date) < pd.to_datetime(performance_start)
            # 性能起点当天：重置 portfolio 为初始资金，开始正常交易
            is_perf_start = performance_start is not None and pd.to_datetime(date) == pd.to_datetime(performance_start)
            
            if is_perf_start:
                # 重置 portfolio 为初始资金，准备开始绩效统计
                portfolio = {
                    'cash': self.initial_capital,
                    'positions': {},
                    'total_value': self.initial_capital,
                }
                cooling_list = {}
                last_rebalance_date = None
                key_dates['performance_start'] = date_str
            
            if is_warmup or is_pre_performance:
                # 只有预热期记录NAV（向后兼容）；pre-performance 期间不记录
                if is_warmup:
                    total_value = portfolio['cash']
                    positions_value = 0
                    
                    # 获取当日基准价格
                    bench_price = None
                    if not bench_df[bench_df['date'] == date].empty:
                        bench_price = bench_df[bench_df['date'] == date]['close'].iloc[0]
                    
                    nav_records.append({
                        'date': date_str,
                        'nav': total_value,
                        'cash': portfolio['cash'],
                        'positions_value': positions_value,
                        'industry_value': 0.0,
                        'defense_value': 0.0,
                        'num_positions': 0,
                        'bench_price': bench_price,
                        'max_total_position': max_total_position,
                        'positions_pct': {},
                        'positions_detail': {},
                    })
                continue
            
            # ========== 每日止损检查（固定止损 + 动态止盈 + early_exit）==========
            # v6.1: 更新days_held
            for ticker, pos in portfolio['positions'].items():
                pos['days_held'] = pos.get('days_held', 0) + 1
            
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
                    
                    # 止损检查（支持固定止损、ATR止损、不止损）
                    stop_loss_mode = self.cfg.get('stop_loss_mode', 'fixed')
                    triggered_stop = False
                    stop_reason = ''
                    
                    if stop_loss_mode == 'fixed':
                        # 固定止损
                        if pnl < self.cfg['stop_loss']:
                            triggered_stop = True
                            stop_reason = '固定止损'
                    
                    elif stop_loss_mode == 'atr':
                        # ATR动态止损
                        atr = pos.get('atr_at_entry', 0)
                        atr_multiplier = self.cfg.get('atr_stop_multiplier', 2.0)
                        if atr > 0 and cost > 0:
                            atr_stop_price = cost - atr_multiplier * atr
                            fixed_stop_price = cost * (1 + self.cfg['stop_loss'])
                            # 取更宽松的（止损价更低的）
                            stop_price = min(atr_stop_price, fixed_stop_price)
                            if current_price < stop_price:
                                triggered_stop = True
                                stop_reason = f'ATR止损({atr_multiplier}xATR={atr:.3f}, 止损价={stop_price:.3f})'
                        else:
                            # 没有ATR数据，回退到固定止损
                            if pnl < self.cfg['stop_loss']:
                                triggered_stop = True
                                stop_reason = '固定止损(无ATR)'
                    
                    # stop_loss_mode == 'none' 不触发止损
                    
                    # v6.1: early_exit检查（仅行业ETF）
                    if early_exit_days is not None and not triggered_stop and ticker in _core_tickers:
                        days_held = pos.get('days_held', 0)
                        if days_held >= early_exit_days:
                            if current_price < cost and cost > 0:
                                triggered_stop = True
                                stop_reason = '3日失败退出'
                    
                    if triggered_stop:
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
                    else:
                        # 动态止盈（实验性v1.2）
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
            slippage = getattr(self, 'slippage_bps', 0) / 10000.0
            for stop in stops:
                ticker = stop['ticker']
                price = stop['current_price'] * (1 - slippage)
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
                rebalance_count += 1
                rebalance_dates.append(date_str)
                
                # 1. 获取BUY信号候选
                buy_signals = day_signals[day_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
                candidates = set(buy_signals['ticker'].tolist())
                
                # v6: 记录首个有效信号日期
                if not buy_signals.empty and key_dates['first_signal_date'] is None:
                    key_dates['first_signal_date'] = date_str
                
                # B1: 排名缓冲机制（单变量测试）
                rank_buffer_enabled = self.cfg.get('rank_buffer_enabled', False)
                buy_rank_n = self.cfg.get('buy_rank_n', None)
                sell_rank_n = self.cfg.get('sell_rank_n', None)
                
                # 预计算所有候选的排名（用于卖出判断）
                candidate_rank = {}
                if rank_buffer_enabled and sell_rank_n is not None:
                    for i, ticker in enumerate(buy_signals['ticker'].tolist()):
                        candidate_rank[ticker] = i + 1
                
                # 独立控制参数（拆分实验）
                exit_debounce = self.cfg.get('exit_debounce', 0)  # 卖出防抖：连续N次调仓跌出候选列表才卖
                min_hold_for_candidate_exit = self.cfg.get('min_hold_for_candidate_exit', 0)  # 最短持有：仅限制候选列表退出
                same_group_max = self.cfg.get('same_group_max_holdings', 0)  # 同类分组：0=不限制
                
                # 获取同类分组映射
                import config as _cfg_module
                etf_group_map = getattr(_cfg_module, 'ETF_GROUP_MAP', {})
                # v2.5 纯函数调仓（顺序独立、总仓位受控）
                if self.cfg.get('use_v2_rebalance', True):
                    self._rebalance_v2(
                        portfolio, day_signals, day_prices, effective_close_prices,
                        last_valid_close, date, date_str, buy_signals, trade_records,
                        cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                        _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                        buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                        min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                        calc_commission, slippage,
                    )
                else:
                    
                    # 1. 卖出逻辑（拆分实验：各参数独立控制）
                    for ticker in list(portfolio['positions'].keys()):
                        if ticker not in day_prices:
                            continue
                        
                        pos = portfolio['positions'][ticker]
                        price = day_prices[ticker]
                        shares = pos['shares']
                        
                        # B1: 排名缓冲卖出逻辑
                        if rank_buffer_enabled and sell_rank_n is not None and ticker in _core_tickers:
                            # 核心池ETF：跌出前sell_rank_n才卖出
                            rank = candidate_rank.get(ticker, len(buy_signals) + 1)
                            in_top_n = rank <= sell_rank_n
                        else:
                            # B0 传统逻辑：检查是否在候选列表
                            in_top_n = ticker in candidates
                        
                        if in_top_n:
                            # 在前N名/候选列表中，重置防抖计数
                            pos['out_candidate_weeks'] = 0
                            continue
                        
                        # 不在前N名中，检查是否满足卖出条件
                        should_sell = True
                        hold_days = (date - pd.to_datetime(pos['entry_date'])).days
                        
                        # B. 卖出防抖：连续N次调仓确认
                        if exit_debounce > 0:
                            pos['out_candidate_weeks'] = pos.get('out_candidate_weeks', 0) + 1
                            if pos['out_candidate_weeks'] < exit_debounce:
                                should_sell = False
                        
                        # C. 最短持有：仅限制"调出候选列表"退出，止损例外
                        if should_sell and min_hold_for_candidate_exit > 0:
                            if hold_days < min_hold_for_candidate_exit:
                                should_sell = False
                        
                        if not should_sell:
                            continue
                        
                        # 执行卖出
                        proceeds = shares * price
                        commission = calc_commission(proceeds)
                        net_proceeds = proceeds - commission
                        
                        portfolio['cash'] += net_proceeds
                        
                        pnl = (price - pos['cost']) / pos['cost']
                        
                        # 确定卖出原因
                        out_weeks = pos.get('out_candidate_weeks', 0)
                        if rank_buffer_enabled and sell_rank_n is not None and ticker in _core_tickers:
                            reason = f'跌出前{sell_rank_n}名（排名{rank}）'
                        elif exit_debounce > 0 and out_weeks >= exit_debounce:
                            reason = f'连续{out_weeks}次跌出候选列表'
                        else:
                            reason = '调出候选列表'
                        
                        trade_records.append({
                            'date': date_str,
                            'ticker': ticker,
                            'action': 'SELL',
                            'price': price,
                            'shares': shares,
                            'amount': proceeds,
                            'commission': commission,
                            'pnl_pct': pnl,
                            'reason': reason
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
                        
                        # 防御模块（v1.3）
                        # 当大盘择时信号低时，强制配置防御资产（黄金/国债）
                        import config as _config_module
                        _defense_tickers = list(_config_module.DEFENSE_UNIVERSE.keys())
                        _core_tickers = [t for t in list(getattr(_config_module, 'CORE_UNIVERSE', {}).keys()) if t not in excluded_tickers]
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
                                                    'high_water': price,
                                                    'days_held': 0,
                                                    'atr_at_entry': day_signals[day_signals['ticker'] == ticker]['atr_14'].iloc[0] if not day_signals[day_signals['ticker'] == ticker].empty and 'atr_14' in day_signals.columns else 0
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
                        
                        # ========== 买入核心池ETF（统一排序 + 相关性去重）==========
                        if max_new > 0:
                            # 核心池买入信号（含行业和概念ETF）
                            core_signals = buy_signals[buy_signals['ticker'].isin(_core_tickers)]
                            
                            # B1: 排名缓冲买入逻辑（只买前buy_rank_n个）
                            if rank_buffer_enabled and buy_rank_n is not None:
                                core_signals = core_signals.head(buy_rank_n)
                            
                            current_core_holdings = sum(1 for t in portfolio['positions'] if t in _core_tickers)
                            core_slots = min(max_new, self.cfg['max_holdings'] - current_core_holdings)
                            
                            # 按总评分排序
                            core_signals = core_signals.sort_values('total_score', ascending=False)
                            
                            # 已选中的核心池ETF（用于相关性去重）
                            selected_core = [t for t in portfolio['positions'] if t in _core_tickers]
                            
                            for _, row in core_signals.iterrows():
                                if core_slots <= 0:
                                    break
                                
                                ticker = row['ticker']
                                
                                # 检查冷却期（实验性v1.2）
                                if ticker in cooling_list:
                                    days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                                    if days_since_stop < self.cfg.get('cooling_period', 0):
                                        continue  # 仍在冷却期内，跳过买入
                                
                                if ticker not in day_prices or ticker in portfolio['positions']:
                                    continue
                                
                                # D. 同类分组检查：只限制买入数量，不强制替换
                                if same_group_max > 0 and ticker in etf_group_map:
                                    ticker_group = etf_group_map[ticker]
                                    # 检查当前持仓中同类组的数量
                                    group_holdings = [t for t in portfolio['positions'] if t in etf_group_map and etf_group_map[t] == ticker_group]
                                    if len(group_holdings) >= same_group_max:
                                        # 同类已满，直接跳过（不替换）
                                        continue
                                
                                # 相关性去重：检查与已选中ETF的相关性
                                if corr_matrix and date in corr_matrix and ticker in corr_matrix[date]:
                                    skip = False
                                    for selected_ticker in selected_core:
                                        if selected_ticker in corr_matrix[date][ticker]:
                                            corr = corr_matrix[date][ticker][selected_ticker]
                                            if corr > corr_threshold:
                                                skip = True
                                                break
                                    if skip:
                                        continue  # 相关性过高，跳过
                                
                                # 冷却期后重新买入需要更高评分（实验性v1.2）
                                min_score = self.cfg['min_total_score']
                                if ticker in cooling_list:
                                    days_since_stop = (pd.to_datetime(date) - pd.to_datetime(cooling_list[ticker])).days
                                    if days_since_stop >= self.cfg.get('cooling_period', 0):
                                        min_score += self.cfg.get('cooling_score_boost', 0)
                                
                                # 评分不达标则跳过
                                if row['total_score'] < min_score:
                                    continue
                                
                                price = day_prices[ticker]
                                
                                # v1.2.1: enhanced 池单只仓位减半（0.075 vs 0.15）
                                if ticker in enhanced_tickers:
                                    # enhanced 池：单只仓位上限减半，且总持仓限制更严格
                                    max_position_for_ticker = min(self.cfg['max_position_per_etf'] / 2, 0.075)
                                    max_total_for_ticker = min(self.cfg['max_holdings'], 4)  # enhanced 总持仓最多4只
                                else:
                                    max_position_for_ticker = self.cfg['max_position_per_etf']
                                    max_total_for_ticker = self.cfg['max_holdings']
                                
                                base_weight = min(max_position_for_ticker, 1.0 / max_total_for_ticker)
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
                                    'high_water': price,  # 初始化最高价（动态止盈用）
                                    'days_held': 0,
                                    'atr_at_entry': day_signals[day_signals['ticker'] == ticker]['atr_14'].iloc[0] if not day_signals[day_signals['ticker'] == ticker].empty and 'atr_14' in day_signals.columns else 0
                                }
                                
                                # 从冷却期列表中移除（已重新买入）
                                if ticker in cooling_list:
                                    del cooling_list[ticker]
                                
                                selected_core.append(ticker)
                                core_slots -= 1
                                max_new -= 1
                                
                                # v6: 记录首笔买入日期
                                if key_dates['first_buy_date'] is None:
                                    key_dates['first_buy_date'] = date_str
                                
                                trade_records.append({
                                    'date': date_str,
                                    'ticker': ticker,
                                    'action': 'BUY',
                                    'price': price,
                                    'shares': shares,
                                    'amount': cost,
                                    'commission': commission,
                                    'pnl_pct': 0,
                                    'reason': f"核心池(评分{row['total_score']:.1f})"
                                })
                        
                        # ========== 备选池兜底（核心池选不满时补充）==========
                        # 备选池兜底（核心池选不满时自动启用）
                        # v1.2.2修复: 检查fallback_equity_enabled配置，确保与实盘一致
                        if (max_new > 0 and available_cash > 1000 and 
                                self.cfg.get('fallback_equity_enabled', False)):
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
                                            'high_water': price,
                                            'atr_at_entry': day_signals[day_signals['ticker'] == ticker]['atr_14'].iloc[0] if not day_signals[day_signals['ticker'] == ticker].empty and 'atr_14' in day_signals.columns else 0
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
                                            'reason': f"备选池兜底(评分{row['total_score']:.1f})"
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
                                        'high_water': price,
                                        'atr_at_entry': day_signals[day_signals['ticker'] == ticker]['atr_14'].iloc[0] if not day_signals[day_signals['ticker'] == ticker].empty and 'atr_14' in day_signals.columns else 0
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
            # v5: 使用有效收盘价（缺失的用最近有效收盘价，禁止归零）
            positions_value = 0
            for ticker, pos in portfolio['positions'].items():
                close_price = effective_close_prices.get(ticker, 0)
                if close_price > 0:
                    positions_value += pos['shares'] * close_price
            
            total_value = portfolio['cash'] + positions_value
            
            # v5: 记录缺失价格事件（旧逻辑会导致持仓市值归零的虚假冲击）
            for ticker, pos in portfolio['positions'].items():
                if ticker not in day_close_prices or pd.isna(day_close_prices.get(ticker)) or day_close_prices.get(ticker, 0) <= 0:
                    if ticker in last_valid_close:
                        missing_price_counter[ticker] = missing_price_counter.get(ticker, 0) + 1
                        last_valid = last_valid_close[ticker]
                        # v5b: impact = 当日新增的 NAV 错误减少额
                        # 第一天：持仓市值被漏算，NAV 减少 = shares * last_valid
                        # 后续天：NAV 已系统性低估，当日新增 = 0
                        if missing_price_counter[ticker] == 1:
                            daily_impact = pos['shares'] * last_valid
                        else:
                            daily_impact = 0.0
                        missing_price_log.append({
                            'ticker': ticker,
                            'date': date_str,
                            'consecutive_missing_days': missing_price_counter[ticker],
                            'last_valid_price': last_valid,
                            'shares': pos['shares'],
                            'impact': daily_impact,
                            'reason': '价格缺失（停牌或数据缺数）',
                        })
            
            # 获取当日基准价格
            bench_price = None
            if not bench_df[bench_df['date'] == date].empty:
                bench_price = bench_df[bench_df['date'] == date]['close'].iloc[0]
            
            # Calculate position allocations（用有效收盘价）
            positions_pct = {}
            if total_value > 0:
                for t, p in portfolio['positions'].items():
                    close_price = effective_close_prices.get(t, 0)
                    if close_price > 0:
                        positions_pct[t] = (p['shares'] * close_price) / total_value
            
            # 构建持仓明细（用于v4逐日归因）——记录所有持仓，含停牌/数据缺失的ETF
            positions_detail = {}
            industry_value = 0.0
            defense_value = 0.0
            for ticker, pos in portfolio['positions'].items():
                close_price = effective_close_prices.get(ticker, 0)
                if close_price > 0:
                    positions_detail[ticker] = {
                        'shares': pos['shares'],
                        'cost': pos['cost'],
                        'entry_date': pos['entry_date'],
                        'high_water': pos.get('high_water', pos['cost']),
                        'market_value': pos['shares'] * close_price,
                    }
                    # 分类统计行业/防御持仓市值
                    if ticker in _defense_tickers:
                        defense_value += pos['shares'] * close_price
                    else:
                        industry_value += pos['shares'] * close_price
                else:
                    # 数据缺失（停牌）：记录持仓但市值为0，确保归因时能看到该持仓
                    positions_detail[ticker] = {
                        'shares': pos['shares'],
                        'cost': pos['cost'],
                        'entry_date': pos['entry_date'],
                        'high_water': pos.get('high_water', pos['cost']),
                        'market_value': 0.0,
                    }
            
            nav_records.append({
                'date': date_str,
                'nav': total_value,
                'cash': portfolio['cash'],
                'positions_value': positions_value,
                'industry_value': industry_value,
                'defense_value': defense_value,
                'num_positions': len(portfolio['positions']),
                'bench_price': bench_price,
                'max_total_position': max_total_position,
                'positions_pct': positions_pct,
                'positions_detail': positions_detail,
            })
        
        # ========== 计算绩效指标 ==========
        nav_df = pd.DataFrame(nav_records)
        if nav_df.empty:
            return {'error': '无回测结果'}
        
        nav_df['date'] = pd.to_datetime(nav_df['date'])
        nav_df = nav_df.sort_values('date')
        
        # v6: 截断预热期，从实际运行起始日计算
        warmup_end_dt = pd.to_datetime(warmup_end)
        if nav_df['date'].min() < warmup_end_dt:
            nav_df = nav_df[nav_df['date'] >= warmup_end_dt].reset_index(drop=True)
        
        # 计算收益率（从截断后的起点）
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
            'buy_count': len(trades_df[trades_df['action'] == 'BUY']) if not trades_df.empty else 0,
            'sell_count': len(trades_df[trades_df['action'].isin(['SELL', 'STOP_LOSS'])]) if not trades_df.empty else 0,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'total_commission': total_commission,
            'stop_loss_count': stop_loss_count,
            'rebalance_count': rebalance_count,
            'rebalance_dates': rebalance_dates,
            'avg_holdings': nav_df['num_positions'].mean(),
            'max_holdings': nav_df['num_positions'].max(),
            'params': self.cfg,
            'missing_price_log': pd.DataFrame(missing_price_log) if missing_price_log else pd.DataFrame(),
            'warmup_info': warmup_info,
            'key_dates': key_dates,
        }
        
        return result
    
    def run_in_sample(self, market_df, bench_df) -> dict:
        """样本内回测
        
        v6: 传入到 in_sample_end 为止的数据，performance_start=None（从统一起点开始）。
        """
        end = BACKTEST_CONFIG['in_sample_end']
        mask = market_df['date'] <= end
        return self.run(market_df[mask], bench_df[bench_df['date'] <= end])

    def run_out_sample(self, market_df, bench_df) -> dict:
        """样本外验证
        
        v6: 传入完整数据，但指定 performance_start=2024-01-01。
            指标计算在完整数据上进行（包含预热），但交易和NAV从2024年开始。
            避免2024年开头因数据被切掉而重新空等51天。
        """
        start = BACKTEST_CONFIG['out_sample_start']
        return self.run(market_df, bench_df, performance_start=start)


if __name__ == '__main__':
    print("回测引擎初始化完成")
    print(f"初始资金: {BACKTEST_CONFIG['initial_capital']:,}")

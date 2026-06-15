"""
ETF Universe Builder - v1.2.1 ETF Pool Governance

核心原则：
  1. 只用决策时点已有的数据（跟踪指数、费率、规模、成分股）
  2. 历史表现（收益、相关性、Sharpe）仅用于验证，不用于规则制定
  3. Walk-forward：每年用当时已知信息重新评估池子
  4. 新ETF默认进入观察池，至少观察2年再评估

事前规则（决策依据）：
  A. 指数去重：跟踪同一指数的ETF只保留费率最低的
  B. 成分重叠：前十大成分重叠>80%视为同一敞口
  C. 规模门槛：规模<5亿排除（流动性风险）
  D. 费率门槛：管理费>0.5%进入备选池
  E. 观察期：上市<2年默认观察池，不参与评分

事后验证（仅验证）：
  V1. 被保留的ETF是否确实表现优于被剔除的？
  V2. 被标记冗余的ETF是否确实高度相关？
  V3. 观察池的ETF是否确实数据不足？

回测可信度边界：
  - 现在表现好≠未来表现好
  - 需要滚动窗口验证规则有效性
  - 小样本ETF（<2年）的信号不可信
"""

import pandas as pd
import numpy as np
import sqlite3
import logging
import json
from datetime import datetime
from pathlib import Path

from config import DB_PATH, CORE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, DEFENSE_UNIVERSE

logger = logging.getLogger(__name__)


class UniverseBuilder:
    """
    ETF池治理器：基于事前规则评估ETF，不依赖未来数据
    
    关键设计：
    - evaluate_at_date(): 在指定日期用当时已知信息评估池子
    - 不是一次性看全历史，而是模拟"当时知道什么"
    - 新增ETF默认观察2年，不直接进核心池
    """
    
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        
        # 事前规则参数（不依赖历史表现）
        self.rules = {
            # 规模门槛：规模太小流动性不足
            'min_scale_yi': 5.0,  # 5亿
            
            # 费率门槛：太高则性价比低
            'max_fee_rate': 0.005,  # 0.5%
            
            # 观察期：上市太短数据不足，不参与评分
            'min_history_days': 500,  # ~2年
            
            # 硬冗余：跟踪指数相同或成分重叠>80%
            'index_overlap_threshold': 0.95,  # 跟踪同一指数
            'holding_overlap_threshold': 0.80,  # 前十大成分重叠
            
            # 冗余对选择：费率优先，其次规模
            'redundancy_preference': ['fee_rate', 'scale', 'history_days'],
        }
        
        # 假设的ETF元数据（实际应从数据库或外部接口获取）
        # 这些是在ETF上市时就可获得的数据
        self._etf_metadata = self._load_or_init_metadata()
    
    def _load_or_init_metadata(self):
        """
        加载ETF元数据（跟踪指数、费率、规模、成分股）
        这些是在ETF上市时就可获得的数据，不是历史回测数据
        """
        metadata_file = Path(self.db_path).parent / 'etf_metadata.json'
        
        if metadata_file.exists():
            with open(metadata_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        
        # 初始化默认元数据（实际应通过API获取）
        # 这些是假设数据，实际项目中应从iFinD/AKShare获取
        metadata = self._build_default_metadata()
        
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        return metadata
    
    def _build_default_metadata(self):
        """构建默认元数据（实际应从数据源获取）"""
        # 跟踪指数映射（关键：同一指数=硬冗余）
        index_map = {
            # Industry ETFs
            '512480.SH': 'CSI Semiconductor',
            '515230.SH': 'CSI Software Services',
            '515880.SH': 'CSI Communication',
            '512010.SH': 'CSI Medical',
            '159928.SZ': 'CSI Consumer',
            '516160.SH': 'CSI New Energy',
            '516110.SH': 'CSI Auto',
            '512800.SH': 'CSI Bank',
            '512000.SH': 'CSI Securities',
            '512660.SH': 'CSI Defense',
            '512980.SH': 'CSI Media',
            '512400.SH': 'CSI Non-ferrous Metal',
            '159996.SZ': 'CSI Home Appliances',
            '159865.SZ': 'CSI Livestock',
            '159697.SZ': 'CSI Oil & Gas',
            '159530.SZ': 'CSI Robotics',
            
            # Concept ETFs
            '588200.SH': 'STAR Chip',
            '159869.SZ': 'CSI Animation & Game',
            '516510.SH': 'CSI Cloud Computing',
            '562500.SH': 'CSI Robotics',  # Same index as 159530.SZ!
            '159740.SZ': 'CSI Environmental',
            '515050.SH': 'CSI 5G Communication',
            '512690.SH': 'CSI Alcohol',
            '515170.SH': 'CSI Food & Beverage',  # High correlation with 512690.SH
            '159766.SZ': 'CSI Tourism',
            '159992.SZ': 'CSI Innovative Medicine',
            '159898.SZ': 'CSI Medical Devices',
            '515790.SH': 'CSI Photovoltaic',
            '159566.SZ': 'CSI Energy Storage',
            '513160.SH': 'CSI HK Tech',
            '510880.SH': 'CSI Dividend',
            '560700.SH': 'CSI SOE Reform',
            
            # Removed 3 ETFs
            '516120.SH': 'CSI Chemical',
            '516960.SH': 'CSI Machinery',
            '516650.SH': 'CSI Non-ferrous Metal Theme',  # Related to 512400.SH
        }
        
        # 前十大成分重叠度（假设数据，实际应从定期报告获取）
        # 0=无重叠，1=完全重叠
        holding_overlap = {
            ('516650.SH', '512400.SH'): 0.85,  # 有色龙头 vs 有色金属：高度重叠
            ('512690.SH', '515170.SH'): 0.90,  # 白酒 vs 食品饮料：高度重叠
            ('159530.SZ', '562500.SH'): 0.88,  # 两个机器人ETF：同指数不同包装
            ('516120.SH', '516160.SH'): 0.60,  # 化工 vs 新能源：部分重叠
            ('516960.SH', '516160.SH'): 0.55,  # 机械 vs 新能源：部分重叠
        }
        
        # 费率（假设数据，实际应从招募说明书获取）
        fee_rates = {
            '512480.SH': 0.0020, '515230.SH': 0.0020, '515880.SH': 0.0015,
            '512010.SH': 0.0020, '159928.SZ': 0.0020, '516160.SH': 0.0020,
            '516110.SH': 0.0020, '512800.SH': 0.0015, '512000.SH': 0.0020,
            '512660.SH': 0.0020, '512980.SH': 0.0020, '512400.SH': 0.0020,
            '159996.SZ': 0.0020, '159865.SZ': 0.0020, '159697.SZ': 0.0020,
            '159530.SZ': 0.0020, '588200.SH': 0.0020, '159869.SZ': 0.0020,
            '516510.SH': 0.0020, '562500.SH': 0.0020, '159740.SZ': 0.0020,
            '515050.SH': 0.0015, '512690.SH': 0.0020, '515170.SH': 0.0020,
            '159766.SZ': 0.0020, '159992.SZ': 0.0020, '159898.SZ': 0.0020,
            '515790.SH': 0.0020, '159566.SZ': 0.0020, '513160.SH': 0.0020,
            '510880.SH': 0.0015, '560700.SH': 0.0020, '516120.SH': 0.0020,
            '516960.SH': 0.0020, '516650.SH': 0.0020,
        }
        
        # 规模（假设数据，实际应从定期报告获取）
        # 单位：亿元
        scales = {
            '512480.SH': 45.0, '515230.SH': 30.0, '515880.SH': 25.0,
            '512010.SH': 80.0, '159928.SZ': 35.0, '516160.SH': 60.0,
            '516110.SH': 40.0, '512800.SH': 120.0, '512000.SH': 90.0,
            '512660.SH': 55.0, '512980.SH': 28.0, '512400.SH': 70.0,
            '159996.SZ': 15.0, '159865.SZ': 12.0, '159697.SZ': 8.0,
            '159530.SZ': 10.0, '588200.SH': 50.0, '159869.SZ': 35.0,
            '516510.SH': 22.0, '562500.SH': 8.0, '159740.SZ': 18.0,
            '515050.SH': 40.0, '512690.SH': 120.0, '515170.SH': 80.0,
            '159766.SZ': 15.0, '159992.SZ': 45.0, '159898.SZ': 20.0,
            '515790.SH': 85.0, '159566.SZ': 6.0, '513160.SH': 12.0,
            '510880.SH': 150.0, '560700.SH': 25.0, '516120.SH': 5.0,
            '516960.SH': 4.0, '516650.SH': 3.0,
        }
        
        # 上市日期（实际可查）
        listing_dates = {
            '512480.SH': '2019-05-16', '515230.SH': '2020-04-24', '515880.SH': '2019-08-16',
            '512010.SH': '2019-04-12', '159928.SZ': '2019-06-12', '516160.SH': '2020-03-20',
            '516110.SH': '2020-04-24', '512800.SH': '2018-07-19', '512000.SH': '2016-08-30',
            '512660.SH': '2016-08-11', '512980.SH': '2019-05-16', '512400.SH': '2017-08-03',
            '159996.SZ': '2020-04-24', '159865.SZ': '2021-03-18', '159697.SZ': '2021-05-07',
            '159530.SZ': '2021-04-16', '588200.SH': '2022-09-30', '159869.SZ': '2021-02-25',
            '516510.SH': '2020-04-24', '562500.SH': '2021-05-07', '159740.SZ': '2020-04-24',
            '515050.SH': '2019-09-17', '512690.SH': '2019-04-12', '515170.SH': '2020-04-24',
            '159766.SZ': '2021-03-18', '159992.SZ': '2020-04-24', '159898.SZ': '2020-04-24',
            '515790.SH': '2020-12-18', '159566.SZ': '2021-03-18', '513160.SH': '2021-05-07',
            '510880.SH': '2006-11-17', '560700.SH': '2021-03-18', '516120.SH': '2021-03-09',
            '516960.SH': '2021-03-18', '516650.SH': '2021-06-21',
        }
        
        metadata = {}
        for ticker in index_map:
            metadata[ticker] = {
                'name': index_map[ticker],
                'tracking_index': index_map[ticker],
                'fee_rate': fee_rates.get(ticker, 0.0020),
                'scale_yi': scales.get(ticker, 10.0),
                'listing_date': listing_dates.get(ticker, '2020-01-01'),
            }
        
        # 添加成分重叠度
        metadata['_holding_overlap'] = {f"{k[0]},{k[1]}": v for k, v in holding_overlap.items()}
        
        return metadata
    
    def evaluate_at_date(self, candidate_tickers, eval_date, market_data_df=None):
        """
        在指定评估日期用当时已知信息评估ETF池
        
        关键：只用eval_date之前已知的数据，不用未来数据
        
        Parameters:
            candidate_tickers: dict {ticker: name}
            eval_date: str 'YYYY-MM-DD'，评估日期
            market_data_df: DataFrame with date/ticker/close/volume
        
        Returns:
            dict with pools and decisions
        """
        eval_dt = pd.to_datetime(eval_date)
        
        decisions = {}
        for ticker in candidate_tickers:
            meta = self._etf_metadata.get(ticker, {})
            decision = self._evaluate_single_et(ticker, meta, eval_dt, candidate_tickers, market_data_df)
            decisions[ticker] = decision
        
        # 处理冗余对（指数相同或成分重叠）
        self._resolve_redundancies(decisions, candidate_tickers, eval_dt)
        
        # 构建池子
        pools = {'core': [], 'fallback': [], 'watch': [], 'excluded': []}
        for ticker, d in decisions.items():
            pools[d['pool']].append(ticker)
        
        return {
            'eval_date': eval_date,
            'decisions': decisions,
            'pools': pools,
            'rules': self.rules,
        }
    
    def _evaluate_single_et(self, ticker, meta, eval_dt, all_tickers, market_df):
        """评估单个ETF"""
        reasons = []
        pool = 'core'  # 默认核心池
        
        # 1. 上市时间检查：上市太短=观察池
        listing_date = pd.to_datetime(meta.get('listing_date', '2020-01-01'))
        history_days = (eval_dt - listing_date).days
        
        if history_days < self.rules['min_history_days']:
            pool = 'watch'
            reasons.append(f"Observation: listed {history_days}d < {self.rules['min_history_days']}d")
        
        # 2. 规模检查：规模太小=排除
        scale = meta.get('scale_yi', 0)
        if scale < self.rules['min_scale_yi']:
            pool = 'excluded'
            reasons.append(f"Scale too small: {scale}yi < {self.rules['min_scale_yi']}yi")
        
        # 3. 费率检查：太高=备选池
        fee = meta.get('fee_rate', 0)
        if fee > self.rules['max_fee_rate']:
            if pool == 'core':  # Only downgrade from core
                pool = 'fallback'
            reasons.append(f"Fee too high: {fee:.2%} > {self.rules['max_fee_rate']:.2%}")
        
        # 4. 成分重叠检查（在_resolve_redundancies中处理）
        # 这里只标记，不单独排除
        
        return {
            'pool': pool,
            'reasons': reasons,
            'ticker': ticker,
            'name': meta.get('name', ''),
            'tracking_index': meta.get('tracking_index', ''),
            'scale_yi': scale,
            'fee_rate': fee,
            'listing_date': str(listing_date.date()),
            'history_days': history_days,
            'meta': meta,
        }
    
    def _resolve_redundancies(self, decisions, all_tickers, eval_dt):
        """解决冗余对：同一指数或高成分重叠"""
        # 1. 指数去重：跟踪同一指数的只保留一个
        index_groups = {}
        for ticker in all_tickers:
            meta = self._etf_metadata.get(ticker, {})
            idx = meta.get('tracking_index', '')
            if idx:
                if idx not in index_groups:
                    index_groups[idx] = []
                index_groups[idx].append(ticker)
        
        for idx, tickers in index_groups.items():
            if len(tickers) > 1:
                # 选择最优：费率低 > 规模大 > 历史长
                best = self._select_best(tickers, eval_dt)
                for t in tickers:
                    if t != best and decisions[t]['pool'] != 'excluded':
                        # 如果另一个没被排除，降级到fallback或watch
                        if decisions[t]['pool'] == 'core':
                            decisions[t]['pool'] = 'fallback'
                        decisions[t]['reasons'].append(
                                                        f"Index redundancy: same index '{idx}' with {best}, keep {best}"
                        )
        
        # 2. 成分重叠去重（即使跟踪不同指数，成分重叠也高）
        overlap_data = self._etf_metadata.get('_holding_overlap', {})
        for pair_str, overlap in overlap_data.items():
            if overlap >= self.rules['holding_overlap_threshold']:
                t1, t2 = pair_str.split(',')
                if t1 in decisions and t2 in decisions:
                    # 选择已上市更久的（更可靠）
                    days1 = decisions[t1]['history_days']
                    days2 = decisions[t2]['history_days']
                    if days1 >= days2:
                        exclude, keep = t2, t1
                    else:
                        exclude, keep = t1, t2
                    
                    if decisions[exclude]['pool'] != 'excluded':
                        if decisions[exclude]['pool'] == 'core':
                            decisions[exclude]['pool'] = 'fallback'
                        decisions[exclude]['reasons'].append(
                            f"Holding redundancy: overlap {overlap:.0%} with {keep}, keep {keep}"
                        )
    
    def _select_best(self, tickers, eval_dt):
        """在冗余组中选择最优ETF"""
        best = None
        best_score = -999
        
        for t in tickers:
            meta = self._etf_metadata.get(t, {})
            score = 0
            
            # 费率低加分
            fee = meta.get('fee_rate', 0.01)
            score += (0.01 - fee) * 1000  # 费率越低分越高
            
            # 规模大加分
            scale = meta.get('scale_yi', 0)
            score += scale * 0.1
            
            # 历史长加分
            listing = pd.to_datetime(meta.get('listing_date', '2020-01-01'))
            days = (eval_dt - listing).days
            score += days * 0.01
            
            if score > best_score:
                best_score = score
                best = t
        
        return best
    
    def walk_forward_validation(self, candidate_tickers, start_date, end_date, step_months=12):
        """
        Walk-forward验证：滚动窗口评估
        
        每年重新评估池子，看规则是否稳定
        
        Returns:
            list of evaluation results at each step
        """
        results = []
        current = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        
        while current <= end:
            eval_date = current.strftime('%Y-%m-%d')
            result = self.evaluate_at_date(candidate_tickers, eval_date)
            results.append(result)
            current += pd.DateOffset(months=step_months)
        
        return results
    
    def generate_report(self, evaluation_result, output_dir='reports'):
        """生成可审计决策报告"""
        import os
        os.makedirs(output_dir, exist_ok=True)
        
        eval_date = evaluation_result['eval_date']
        
        # 1. 决策CSV
        rows = []
        for ticker, d in evaluation_result['decisions'].items():
            rows.append({
                'ticker': ticker,
                'name': d['name'],
                'pool': d['pool'],
                'reason': ' | '.join(d['reasons']),
                'tracking_index': d['tracking_index'],
                'scale_yi': d['scale_yi'],
                'fee_rate': d['fee_rate'],
                'listing_date': d['listing_date'],
                'history_days': d['history_days'],
            })
        
        df = pd.DataFrame(rows)
        df.to_csv(f'{output_dir}/universe_governance_{eval_date}.csv', 
                  index=False, encoding='utf-8-sig')
        
        # 2. 报告Markdown
        pools = evaluation_result['pools']
        with open(f'{output_dir}/universe_governance_{eval_date}.md', 'w', encoding='utf-8') as f:
            f.write(f"# ETF Pool Governance Report ({eval_date})\n\n")
            f.write(f"**Rules:**\n")
            f.write(f"- Min scale: {self.rules['min_scale_yi']}亿\n")
            f.write(f"- Max fee: {self.rules['max_fee_rate']:.2%}\n")
            f.write(f"- Min history: {self.rules['min_history_days']} days\n")
            f.write(f"- Index overlap threshold: {self.rules['index_overlap_threshold']:.0%}\n")
            f.write(f"- Holding overlap threshold: {self.rules['holding_overlap_threshold']:.0%}\n\n")
            
            f.write(f"**Pool Summary:**\n")
            f.write(f"- Core: {len(pools['core'])}\n")
            f.write(f"- Fallback: {len(pools['fallback'])}\n")
            f.write(f"- Watch: {len(pools['watch'])}\n")
            f.write(f"- Excluded: {len(pools['excluded'])}\n\n")
            
            for pool_name in ['core', 'fallback', 'watch', 'excluded']:
                if pools[pool_name]:
                    f.write(f"## {pool_name.upper()} Pool\n\n")
                    for t in pools[pool_name]:
                        d = evaluation_result['decisions'][t]
                        f.write(f"- **{t}** ({d['name']}): ")
                        if d['reasons']:
                            f.write(f"{' | '.join(d['reasons'])}\n")
                        else:
                            f.write(f"OK\n")
                    f.write(f"\n")
        
        return {
            'csv': f'{output_dir}/universe_governance_{eval_date}.csv',
            'md': f'{output_dir}/universe_governance_{eval_date}.md',
        }


if __name__ == '__main__':
    # 演示：在2021-06-01评估35只实验池
    builder = UniverseBuilder()
    
    experimental_pool = {**CORE_UNIVERSE}
    experimental_pool['516120.SH'] = 'Chemical ETF'
    experimental_pool['516960.SH'] = 'Machinery ETF'
    experimental_pool['516650.SH'] = 'Non-ferrous Metal ETF'
    
    # 在2021-06-01评估（当时已知的数据）
    result = builder.evaluate_at_date(experimental_pool, '2021-06-01')
    files = builder.generate_report(result)
    
    print(f"=== Evaluation at 2021-06-01 ===")
    pools = result['pools']
    print(f"Core: {len(pools['core'])} ETFs")
    print(f"Fallback: {len(pools['fallback'])} ETFs")
    print(f"Watch: {len(pools['watch'])} ETFs")
    print(f"Excluded: {len(pools['excluded'])} ETFs")
    
    print(f"\n=== Core Pool ===")
    for t in pools['core']:
        d = result['decisions'][t]
        print(f"  {t}: {d['name']}")
    
    print(f"\n=== Fallback (redundancy) ===")
    for t in pools['fallback']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    print(f"\n=== Watch (too new) ===")
    for t in pools['watch']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    print(f"\n=== Excluded (too small) ===")
    for t in pools['excluded']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    print(f"\nReports: {files}")
    
    # Walk-forward: 看池子如何随时间变化
    print(f"\n=== Walk-forward (2021-06 to 2023-06) ===")
    wf = builder.walk_forward_validation(experimental_pool, '2021-06-01', '2023-06-01', step_months=12)
    for r in wf:
        print(f"\n{r['eval_date']}: Core={len(r['pools']['core'])}, Fallback={len(r['pools']['fallback'])}, Watch={len(r['pools']['watch'])}, Excluded={len(r['pools']['excluded'])}")
        new_watch = [t for t in r['pools']['watch'] if r['eval_date'] == '2021-06-01' or t not in [x for step in wf if step['eval_date'] < r['eval_date'] for x in step['pools']['watch']]]
        if new_watch:
            print(f"  New to watch: {new_watch}")
        new_core = [t for t in r['pools']['core'] if t not in [x for step in wf if step['eval_date'] < r['eval_date'] for x in step['pools']['core']]]
        if new_core:
            print(f"  New to core: {new_core}")

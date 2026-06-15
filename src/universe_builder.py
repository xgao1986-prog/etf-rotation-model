"""
ETF Universe Builder - v1.2.1 ETF Pool Governance

Purpose:
  - Quantify human screening logic into reproducible rules
  - Evaluate candidate ETFs for inclusion/exclusion from core trading pool
  - Generate auditable decision logs for every ETF in the pool

Rules:
  1. Hard Redundancy (>=0.97): Same index, different wrapper -> keep one
  2. Soft Redundancy (0.85-0.97): High correlation but different theme -> soft penalty
  3. Quality Filter: Negative Sharpe / high beta-only -> downgrade to fallback
  4. Liquidity Filter: Low volume -> exclude
  5. History Filter: Less than 2 years -> observe only

Output:
  - Core pool (participates in scoring)
  - Fallback pool (backup only)
  - Watch pool (observe, not trade)
  - Excluded pool (with reasons)
"""

import pandas as pd
import numpy as np
import sqlite3
import logging
from datetime import datetime

from config import DB_PATH, CORE_UNIVERSE, FALLBACK_EQUITY_UNIVERSE, DEFENSE_UNIVERSE

logger = logging.getLogger(__name__)


class UniverseBuilder:
    """ETF池构建器：评估候选ETF，输出核心池/观察池/剔除池"""
    
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.rules = {
            'hard_redundancy_threshold': 0.97,
            'soft_redundancy_threshold_min': 0.85,
            'soft_redundancy_threshold_max': 0.97,
            'soft_penalty_max': 0.15,
            'min_history_days': 500,  # ~2 years
            'min_sharpe_for_core': -0.05,  # Slightly negative allowed
            'min_avg_volume': 1_000_000,  # Minimum daily volume
        }
    
    def evaluate_pool(self, candidate_tickers, lookback_start='2021-06-01', 
                      lookback_end='2026-06-05', reference_benchmark='000300.SH'):
        """
        Evaluate candidate ETF pool and output governance decisions
        
        Parameters:
            candidate_tickers: dict {ticker: name}
            lookback_start: Start date for evaluation
            lookback_end: End date for evaluation
            reference_benchmark: Benchmark for excess return calculation
        
        Returns:
            dict with pools and decision log
        """
        conn = sqlite3.connect(self.db_path)
        
        # 1. Get all market data
        all_tickers = list(candidate_tickers.keys()) + [reference_benchmark]
        placeholders = ','.join(['?'] * len(all_tickers))
        query = f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM market_data
            WHERE ticker IN ({placeholders})
            AND date >= ? AND date <= ?
        """
        params = all_tickers + [lookback_start, lookback_end]
        df = pd.read_sql(query, conn, params=params)
        conn.close()
        
        if df.empty:
            return {'error': 'No data available'}
        
        # 2. Calculate per-ETF metrics
        metrics = self._calculate_metrics(df, candidate_tickers, reference_benchmark)
        
        # 3. Calculate pairwise correlations
        correlations = self._calculate_correlations(df, candidate_tickers)
        
        # 4. Apply governance rules
        decisions = self._apply_governance_rules(metrics, correlations, candidate_tickers)
        
        # 5. Build pools
        pools = {
            'core': [],
            'fallback': [],
            'watch': [],
            'excluded': [],
        }
        
        for ticker, decision in decisions.items():
            pools[decision['pool']].append(ticker)
        
        return {
            'metrics': metrics,
            'correlations': correlations,
            'decisions': decisions,
            'pools': pools,
            'rules': self.rules,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    
    def _calculate_metrics(self, df, candidate_tickers, benchmark):
        """Calculate per-ETF quality metrics"""
        metrics = {}
        
        for ticker in candidate_tickers:
            tdf = df[df['ticker'] == ticker].sort_values('date')
            if len(tdf) < 50:
                metrics[ticker] = {
                    'name': candidate_tickers[ticker],
                    'days': len(tdf),
                    'valid': False,
                    'reason': 'Insufficient data',
                }
                continue
            
            tdf['returns'] = tdf['close'].pct_change(fill_method=None)
            tdf = tdf.dropna(subset=['returns'])
            
            # Annual return
            total_ret = tdf['close'].iloc[-1] / tdf['close'].iloc[0] - 1
            years = len(tdf) / 252
            ann_ret = (1 + total_ret) ** (1/years) - 1 if years > 0 and total_ret > -1 else -1
            
            # Volatility
            vol = tdf['returns'].std() * np.sqrt(252)
            
            # Sharpe
            sharpe = ann_ret / vol if vol > 0 else 0
            
            # Max drawdown
            cum = (1 + tdf['returns']).cumprod()
            peak = cum.cummax()
            dd = (cum - peak) / peak
            max_dd = dd.min()
            
            # Average volume
            avg_volume = tdf['volume'].mean()
            
            # Benchmark correlation and excess return
            bdf = df[df['ticker'] == benchmark].sort_values('date')
            if len(bdf) > 0:
                bdf['returns'] = bdf['close'].pct_change(fill_method=None)
                bdf = bdf.dropna(subset=['returns'])
                bdf = bdf[bdf['date'].isin(tdf['date'])]
                tdf_aligned = tdf[tdf['date'].isin(bdf['date'])]
                if len(tdf_aligned) > 30 and len(bdf) > 0:
                    corr_bench = tdf_aligned['returns'].corr(bdf['returns'].iloc[:len(tdf_aligned)])
                    excess = tdf_aligned['returns'].values - bdf['returns'].iloc[:len(tdf_aligned)].values
                    mean_excess = np.mean(excess) * 252
                    std_excess = np.std(excess) * np.sqrt(252)
                    ir = mean_excess / std_excess if std_excess > 0 else 0
                else:
                    corr_bench = 0
                    ir = 0
            else:
                corr_bench = 0
                ir = 0
            
            # Trend stability
            ret_20 = tdf['close'].pct_change(20).dropna()
            trend_stability = (ret_20 > 0).mean() if len(ret_20) > 0 else 0
            
            metrics[ticker] = {
                'name': candidate_tickers[ticker],
                'days': len(tdf),
                'valid': True,
                'annual_return': ann_ret,
                'volatility': vol,
                'sharpe': sharpe,
                'max_drawdown': max_dd,
                'avg_volume': avg_volume,
                'corr_bench': corr_bench,
                'info_ratio': ir,
                'trend_stability': trend_stability,
            }
        
        return metrics
    
    def _calculate_correlations(self, df, candidate_tickers):
        """Calculate pairwise rolling correlations"""
        tickers = list(candidate_tickers.keys())
        pivot = df[df['ticker'].isin(tickers)].pivot_table(
            index='date', columns='ticker', values='close'
        )
        returns = pivot.pct_change(fill_method=None)
        
        min_valid = int(60 * 0.67)
        correlations = {}
        
        for i, t1 in enumerate(tickers):
            for t2 in tickers[i+1:]:
                if t1 not in returns or t2 not in returns:
                    continue
                rolling_corr = returns[t1].rolling(60, min_periods=min_valid).corr(returns[t2])
                valid = rolling_corr.dropna()
                if len(valid) > 0:
                    correlations[(t1, t2)] = {
                        'mean': valid.mean(),
                        'max': valid.max(),
                        'min': valid.min(),
                        'std': valid.std(),
                    }
        
        return correlations
    
    def _apply_governance_rules(self, metrics, correlations, candidate_tickers):
        """Apply governance rules to determine pool assignment"""
        decisions = {}
        tickers = list(candidate_tickers.keys())
        
        # Initialize
        for t in tickers:
            decisions[t] = {
                'pool': 'core',  # Default
                'reasons': [],
                'max_corr_peer': 0,
                'max_corr_value': 0,
                'penalty': 0,
            }
        
        # Rule 1: Hard Redundancy (>=0.97)
        # Find redundancy groups and keep the best representative
        excluded = set()
        for (t1, t2), corr_data in correlations.items():
            if corr_data['mean'] >= self.rules['hard_redundancy_threshold']:
                if t1 in excluded or t2 in excluded:
                    continue
                # Compare quality metrics
                m1 = metrics.get(t1, {})
                m2 = metrics.get(t2, {})
                
                if not m1.get('valid', False):
                    exclude = t1
                elif not m2.get('valid', False):
                    exclude = t2
                elif m1.get('days', 0) > m2.get('days', 0):
                    exclude = t2
                elif m2.get('days', 0) > m1.get('days', 0):
                    exclude = t1
                elif m1.get('sharpe', -999) > m2.get('sharpe', -999):
                    exclude = t2
                else:
                    exclude = t1
                
                excluded.add(exclude)
                keep = t2 if exclude == t1 else t1
                decisions[exclude]['pool'] = 'excluded'
                decisions[exclude]['reasons'].append(
                    f"Hard redundancy: corr={corr_data['mean']:.4f} with {keep}, keep {keep}"
                )
                decisions[exclude]['max_corr_peer'] = keep
                decisions[exclude]['max_corr_value'] = corr_data['mean']
        
        # Rule 2: Quality Filter (Sharpe too low)
        for t in tickers:
            if t in excluded:
                continue
            m = metrics.get(t, {})
            if m.get('valid', False) and m.get('sharpe', 0) < self.rules['min_sharpe_for_core']:
                decisions[t]['pool'] = 'watch'
                decisions[t]['reasons'].append(
                    f"Low Sharpe: {m.get('sharpe', 0):.3f} < {self.rules['min_sharpe_for_core']}"
                )
        
        # Rule 3: History Filter (too short)
        for t in tickers:
            if t in excluded:
                continue
            m = metrics.get(t, {})
            if m.get('days', 0) < self.rules['min_history_days']:
                # Only downgrade if not already downgraded
                if decisions[t]['pool'] == 'core':
                    decisions[t]['pool'] = 'watch'
                decisions[t]['reasons'].append(
                    f"Short history: {m.get('days', 0)}d < {self.rules['min_history_days']}d"
                )
        
        # Rule 4: Soft Redundancy (0.85-0.97) - Penalty only, no exclusion
        for (t1, t2), corr_data in correlations.items():
            if corr_data['mean'] >= self.rules['hard_redundancy_threshold']:
                continue  # Already handled
            if corr_data['mean'] >= self.rules['soft_redundancy_threshold_min']:
                # Apply penalty to both, but only if they're in core
                for t in [t1, t2]:
                    if t in excluded:
                        continue
                    if decisions[t]['pool'] == 'core':
                        corr = corr_data['mean']
                        penalty = self.rules['soft_penalty_max'] * (
                            (corr - self.rules['soft_redundancy_threshold_min']) /
                            (self.rules['soft_redundancy_threshold_max'] - self.rules['soft_redundancy_threshold_min'])
                        )
                        if penalty > decisions[t]['penalty']:
                            decisions[t]['penalty'] = penalty
                            decisions[t]['reasons'].append(
                                f"Soft penalty: corr={corr:.4f} with {t2 if t==t1 else t1}, penalty={penalty:.2%}"
                            )
        
        return decisions
    
    def generate_report(self, evaluation_result, output_dir='reports'):
        """Generate auditable decision reports"""
        import os
        os.makedirs(output_dir, exist_ok=True)
        
        # 1. Decision log (CSV)
        decision_rows = []
        for ticker, decision in evaluation_result['decisions'].items():
            m = evaluation_result['metrics'].get(ticker, {})
            row = {
                'ticker': ticker,
                'name': m.get('name', ''),
                'pool': decision['pool'],
                'reason': ' | '.join(decision['reasons']),
                'sharpe': m.get('sharpe', 0),
                'annual_return': m.get('annual_return', 0),
                'max_drawdown': m.get('max_drawdown', 0),
                'corr_bench': m.get('corr_bench', 0),
                'info_ratio': m.get('info_ratio', 0),
                'days': m.get('days', 0),
                'penalty': decision['penalty'],
                'max_corr_peer': decision['max_corr_peer'],
                'max_corr_value': decision['max_corr_value'],
            }
            decision_rows.append(row)
        
        df_decisions = pd.DataFrame(decision_rows)
        df_decisions.to_csv(f'{output_dir}/universe_decisions.csv', index=False, encoding='utf-8-sig')
        
        # 2. Redundancy pairs (CSV)
        pair_rows = []
        for (t1, t2), corr_data in evaluation_result['correlations'].items():
            pair_rows.append({
                'ticker1': t1,
                'ticker2': t2,
                'mean_corr': corr_data['mean'],
                'max_corr': corr_data['max'],
                'min_corr': corr_data['min'],
                'std_corr': corr_data['std'],
                'action': 'hard_exclude' if corr_data['mean'] >= 0.97 else (
                    'soft_penalty' if corr_data['mean'] >= 0.85 else 'keep'
                ),
            })
        
        df_pairs = pd.DataFrame(pair_rows)
        df_pairs.to_csv(f'{output_dir}/universe_redundancy_pairs.csv', index=False, encoding='utf-8-sig')
        
        # 3. Summary report (Markdown)
        pools = evaluation_result['pools']
        with open(f'{output_dir}/universe_report.md', 'w', encoding='utf-8') as f:
            f.write(f"# ETF Universe Governance Report\n\n")
            f.write(f"Generated: {evaluation_result['timestamp']}\n\n")
            f.write(f"## Pool Summary\n\n")
            f.write(f"- Core: {len(pools['core'])} ETFs\n")
            f.write(f"- Fallback: {len(pools['fallback'])} ETFs\n")
            f.write(f"- Watch: {len(pools['watch'])} ETFs\n")
            f.write(f"- Excluded: {len(pools['excluded'])} ETFs\n\n")
            
            f.write(f"## Core Pool\n\n")
            for t in pools['core']:
                m = evaluation_result['metrics'].get(t, {})
                f.write(f"- {t} ({m.get('name', '')}): Sharpe={m.get('sharpe', 0):.2f}, Return={m.get('annual_return', 0):.1%}\n")
            
            f.write(f"\n## Excluded Pool\n\n")
            for t in pools['excluded']:
                d = evaluation_result['decisions'].get(t, {})
                f.write(f"- {t}: {' | '.join(d.get('reasons', []))}\n")
            
            f.write(f"\n## Watch Pool\n\n")
            for t in pools['watch']:
                d = evaluation_result['decisions'].get(t, {})
                f.write(f"- {t}: {' | '.join(d.get('reasons', []))}\n")
        
        logger.info(f"Universe reports saved to {output_dir}/")
        return {
            'decisions_csv': f'{output_dir}/universe_decisions.csv',
            'pairs_csv': f'{output_dir}/universe_redundancy_pairs.csv',
            'report_md': f'{output_dir}/universe_report.md',
        }


if __name__ == '__main__':
    # Quick test
    builder = UniverseBuilder()
    
    # Test with 35-ETF experimental pool
    experimental_pool = {**CORE_UNIVERSE}
    experimental_pool['516120.SH'] = 'Chemical ETF'
    experimental_pool['516960.SH'] = 'Machinery ETF'
    experimental_pool['516650.SH'] = 'Non-ferrous Metal ETF'
    
    result = builder.evaluate_pool(experimental_pool)
    files = builder.generate_report(result)
    
    print(f"Core pool: {len(result['pools']['core'])} ETFs")
    print(f"Excluded: {len(result['pools']['excluded'])} ETFs")
    print(f"Watch: {len(result['pools']['watch'])} ETFs")
    for t in result['pools']['excluded']:
        d = result['decisions'][t]
        print(f"  {t}: {' | '.join(d['reasons'])}")
    
    print(f"\nReports: {files}")

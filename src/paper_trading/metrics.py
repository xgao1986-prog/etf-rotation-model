# src/paper_trading/metrics.py — performance and comparison metrics for virtual accounts.
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd


def _to_df(nav_history: Sequence[Mapping]) -> pd.DataFrame:
    if not nav_history:
        return pd.DataFrame(columns=['nav_date', 'nav'])
    df = pd.DataFrame(list(nav_history))
    df['nav_date'] = pd.to_datetime(df['nav_date'])
    df = df.sort_values('nav_date').set_index('nav_date')
    return df


def calculate_account_metrics(
    nav_history: Sequence[Mapping],
    trades: Sequence[Mapping],
    risk_free_rate: float = 0.0,
) -> Dict[str, float]:
    """Calculate NAV-based performance metrics.

    Parameters
    ----------
    nav_history:
        Sequence of rows with at least 'nav_date' and 'nav' keys.
    trades:
        Sequence of trade rows with 'trade_date', 'ticker', 'action',
        'shares', 'price', and 'commission' keys.
    risk_free_rate:
        Annual risk-free rate used in Sharpe calculation.

    Returns
    -------
    dict with total_return, annualized_return, sharpe, max_drawdown, calmar,
    total_commission, trade_count, turnover, win_rate, monthly_win_rate.
    """
    df = _to_df(nav_history)
    if len(df) < 2:
        return {
            'total_return': 0.0,
            'annualized_return': 0.0,
            'sharpe': np.nan,
            'max_drawdown': 0.0,
            'calmar': np.nan,
            'total_commission': 0.0,
            'trade_count': 0,
            'turnover': 0.0,
            'win_rate': np.nan,
            'monthly_win_rate': np.nan,
        }

    start_nav = float(df['nav'].iloc[0])
    end_nav = float(df['nav'].iloc[-1])
    total_return = (end_nav - start_nav) / start_nav

    trading_days = len(df)
    years = max(trading_days / 252, 1 / 252)
    annualized_return = (end_nav / start_nav) ** (1 / years) - 1

    returns = df['nav'].pct_change().dropna()
    excess_returns = returns - risk_free_rate / 252
    sharpe = (
        np.sqrt(252) * excess_returns.mean() / returns.std()
        if returns.std() > 1e-12 else np.nan
    )

    cumulative = df['nav'] / start_nav
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    max_drawdown = float(drawdown.min())

    calmar = annualized_return / abs(max_drawdown) if max_drawdown != 0 else np.nan

    total_commission = sum(t.get('commission', 0) or 0 for t in trades)
    trade_count = len(trades)

    traded_value = sum(
        t.get('shares', 0) * t.get('price', 0)
        for t in trades
    )
    avg_nav = float(df['nav'].mean())
    turnover = traded_value / avg_nav if avg_nav > 0 else 0.0

    win_rate = calculate_closed_trade_win_rate(trades)

    monthly = df['nav'].resample('ME').last().dropna()
    if len(monthly) >= 2:
        monthly_returns = monthly.pct_change().dropna()
        monthly_win_rate = float((monthly_returns > 0).mean())
    else:
        monthly_win_rate = np.nan

    return {
        'total_return': total_return,
        'annualized_return': annualized_return,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'calmar': calmar,
        'total_commission': total_commission,
        'trade_count': trade_count,
        'turnover': turnover,
        'win_rate': win_rate,
        'monthly_win_rate': monthly_win_rate,
    }


def calculate_closed_trade_win_rate(trades: Sequence[Mapping]) -> float:
    """Compute win rate from closed buy/sell round trips per ticker.

    A round trip is matched by FIFO per ticker: each BUY increases inventory,
    each SELL/STOP_LOSS decreases it. The P&L of a closed lot is
    (sell_price - buy_price) * shares.
    """
    lots: Dict[str, List[tuple]] = defaultdict(list)  # ticker -> [(buy_price, shares)]
    closed_pnl: List[float] = []

    for trade in sorted(trades, key=lambda t: t.get('trade_date', '')):
        ticker = trade.get('ticker')
        action = trade.get('action', '')
        shares = int(trade.get('shares', 0))
        price = float(trade.get('price', 0))
        if shares <= 0 or price <= 0:
            continue

        if action == 'BUY':
            lots[ticker].append((price, shares))
        elif action in ('SELL', 'STOP_LOSS'):
            remaining = shares
            while remaining > 0 and lots[ticker]:
                buy_price, buy_shares = lots[ticker][0]
                closed_shares = min(remaining, buy_shares)
                closed_pnl.append((price - buy_price) * closed_shares)
                remaining -= closed_shares
                if closed_shares >= buy_shares:
                    lots[ticker].pop(0)
                else:
                    lots[ticker][0] = (buy_price, buy_shares - closed_shares)

    if not closed_pnl:
        return np.nan
    wins = sum(1 for pnl in closed_pnl if pnl > 0)
    return wins / len(closed_pnl)


def build_account_comparison(
    accounts_metrics: Mapping[str, Mapping[str, float]],
    reference_name: str = 'B0.4',
) -> pd.DataFrame:
    """Build a comparison DataFrame with absolute metrics and differences vs reference.

    Parameters
    ----------
    accounts_metrics:
        Mapping of account name/ID -> metrics dict from calculate_account_metrics.
    reference_name:
        Name of the reference account (default 'B0.4').

    Returns
    -------
    DataFrame indexed by account name with absolute metrics and '_diff' columns
    relative to the reference.
    """
    df = pd.DataFrame.from_dict(accounts_metrics, orient='index')
    numeric_cols = [
        'total_return', 'annualized_return', 'sharpe',
        'max_drawdown', 'calmar', 'total_commission',
        'trade_count', 'turnover', 'win_rate', 'monthly_win_rate',
    ]
    for col in numeric_cols:
        if col not in df.columns:
            df[col] = np.nan

    if reference_name in df.index:
        ref = df.loc[reference_name]
        for col in numeric_cols:
            df[f'{col}_diff'] = df[col] - ref[col]
    else:
        for col in numeric_cols:
            df[f'{col}_diff'] = np.nan

    return df

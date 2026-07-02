# src/paper_trading/calendar.py — A-share trading calendar with local cache
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Iterable, Optional, Set


class ChinaTradingCalendar:
    """
    A-share trading calendar.

    Loads trading days from ``src/paper_trading/cn_trading_calendar.csv``.
    If the cache is missing, attempts to fetch from akshare and persist it.
    Falls back to a built-in holiday list for 2024-2027 if both fail.
    """

    _CACHE_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "cn_trading_calendar.csv",
    )

    # Built-in non-trading days for 2024-2027 (covers common holidays)
    _FALLBACK_HOLIDAYS: Set[str] = {
        # 2024
        "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
        "2024-02-15", "2024-02-16", "2024-04-04", "2024-04-05", "2024-05-01",
        "2024-05-02", "2024-05-03", "2024-06-10", "2024-09-16", "2024-09-17",
        "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
        # 2025
        "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
        "2025-02-03", "2025-04-04", "2025-05-01", "2025-05-02", "2025-05-05",
        "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06",
        "2025-10-07", "2025-10-08",
        # 2026 (incl. Dragon Boat: 6-19~6-21)
        "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17",
        "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23", "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19", "2026-06-20",
        "2026-06-21", "2026-09-21", "2026-09-22", "2026-10-01", "2026-10-02",
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        # 2027
        "2027-01-01", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
        "2027-02-12", "2027-02-15", "2027-02-16", "2027-04-05", "2027-05-03",
        "2027-05-04", "2027-05-05", "2027-06-14", "2027-09-20", "2027-09-21",
        "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
        "2027-10-08",
    }

    def __init__(self, trading_days: Optional[Iterable[str]] = None):
        if trading_days is not None:
            self._days: Set[str] = set(str(d) for d in trading_days)
        else:
            self._days = self._load_days()

    @classmethod
    def _load_days(cls) -> Set[str]:
        days: Set[str] = set()
        if os.path.exists(cls._CACHE_FILE):
            try:
                with open(cls._CACHE_FILE, "r", encoding="utf-8") as f:
                    next(f, None)  # skip header
                    for line in f:
                        line = line.strip()
                        if line:
                            days.add(line.split(",")[0])
                if days:
                    return days
            except Exception:
                pass

        try:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            days = set(df["trade_date"].astype(str).tolist())
            cls._save_days(days)
            return days
        except Exception:
            pass

        # Fallback: all weekdays minus built-in holidays within range
        start = datetime(2024, 1, 1)
        end = datetime(2027, 12, 31)
        cur = start
        while cur <= end:
            s = cur.strftime("%Y-%m-%d")
            if cur.weekday() < 5 and s not in cls._FALLBACK_HOLIDAYS:
                days.add(s)
            cur += timedelta(days=1)
        return days

    @classmethod
    def _save_days(cls, days: Iterable[str]) -> None:
        try:
            os.makedirs(os.path.dirname(cls._CACHE_FILE), exist_ok=True)
            with open(cls._CACHE_FILE, "w", encoding="utf-8") as f:
                f.write("trade_date\n")
                for d in sorted(days):
                    f.write(f"{d}\n")
        except Exception:
            pass

    def is_trading_day(self, date: str) -> bool:
        return date in self._days

    def next_trading_day(self, date: str) -> str:
        dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        for _ in range(365 * 3):
            s = dt.strftime("%Y-%m-%d")
            if s in self._days:
                return s
            dt += timedelta(days=1)
        raise RuntimeError(f"no next trading day found after {date}")

    def previous_trading_day(self, date: str) -> str:
        dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
        for _ in range(365 * 3):
            s = dt.strftime("%Y-%m-%d")
            if s in self._days:
                return s
            dt -= timedelta(days=1)
        raise RuntimeError(f"no previous trading day found before {date}")

    def is_rebalance_day(self, date: str) -> bool:
        """Thursday is the rebalance day."""
        return datetime.strptime(date, "%Y-%m-%d").weekday() == 3

    @property
    def min_date(self) -> str:
        return min(self._days) if self._days else ""

    @property
    def max_date(self) -> str:
        return max(self._days) if self._days else ""

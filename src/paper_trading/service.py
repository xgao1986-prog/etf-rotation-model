# src/paper_trading/service.py — account creation, opening balances, ledger writes, and reconciliation
from __future__ import annotations

from datetime import datetime

from .models import (
    AccountCreate,
    AccountStatus,
    StartMode,
    canonical_config,
    config_hash,
)


class PaperTradingService:
    NAV_TOLERANCE = 0.01

    def __init__(self, store):
        self.store = store

    def create_account(self, request: AccountCreate):
        if self.store.get_account(request.account_id):
            raise ValueError(f"account already exists: {request.account_id}")

        positions_value = sum(
            position.market_value for position in request.opening_positions
        )
        opening_cash = (
            request.initial_capital
            if request.start_mode is StartMode.CASH
            else float(request.opening_cash)
        )
        opening_nav = opening_cash + positions_value
        if abs(opening_nav - request.initial_capital) > self.NAV_TOLERANCE:
            raise ValueError(
                f"opening NAV mismatch: {opening_nav:.2f} "
                f"!= {request.initial_capital:.2f}"
            )

        now = datetime.now().isoformat(timespec="seconds")
        account_row = {
            "account_id": request.account_id,
            "name": request.name,
            "account_type": request.account_type.value,
            "group_id": request.group_id,
            "strategy_name": request.strategy_name,
            "config_json": canonical_config(request.strategy_config),
            "config_hash": config_hash(request.strategy_config),
            "initial_capital": request.initial_capital,
            "start_mode": request.start_mode.value,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "status": AccountStatus.READY.value,
            "created_at": now,
            "updated_at": now,
        }

        position_rows = [
            {
                "account_id": request.account_id,
                "as_of_date": request.start_date,
                "ticker": position.ticker,
                "shares": position.shares,
                "cost_price": position.cost_price,
                "last_price": position.last_price,
                "market_value": position.market_value,
            }
            for position in request.opening_positions
        ]
        nav_row = {
            "account_id": request.account_id,
            "nav_date": request.start_date,
            "cash": opening_cash,
            "positions_value": positions_value,
            "nav": opening_nav,
            "data_date": request.start_date,
            "created_at": now,
        }
        self.store.create_account_snapshot(account_row, position_rows, nav_row)
        return self.store.get_account(request.account_id)

    def get_nav(self, account_id, nav_date):
        return self.store.get_nav(account_id, nav_date)

    def replace_config(self, account_id, new_config):
        if not self.store.get_account(account_id):
            raise KeyError(account_id)
        raise RuntimeError(
            "account configuration is immutable; copy to a new account"
        )

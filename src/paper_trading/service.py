# src/paper_trading/service.py — account creation, opening balances, ledger writes, and reconciliation
from __future__ import annotations

from datetime import datetime

from .models import (
    AccountCreate,
    AccountStatus,
    AccountType,
    ManualFill,
    OrderStatus,
    StartMode,
    canonical_config,
    config_hash,
)
from .store import DuplicateLedgerEvent


class PaperTradingService:
    NAV_TOLERANCE = 0.01

    def __init__(self, store):
        self.store = store

    def create_account(self, request: AccountCreate, conn=None):
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
        self.store.create_account_snapshot(account_row, position_rows, nav_row, conn=conn)
        return self.store.get_account(request.account_id)

    def get_nav(self, account_id, nav_date):
        return self.store.get_nav(account_id, nav_date)

    def replace_config(self, account_id, new_config):
        if not self.store.get_account(account_id):
            raise KeyError(account_id)
        raise RuntimeError(
            "account configuration is immutable; copy to a new account"
        )

    def append_order(self, order):
        row = dict(order)
        row["created_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            self.store.append_order(row)
        except DuplicateLedgerEvent as exc:
            raise RuntimeError(f"duplicate ledger event: {exc}") from exc

    def reconcile(self, account_id, nav_date):
        nav = self.store.get_nav(account_id, nav_date)
        if nav is None:
            raise KeyError(f"missing NAV: {account_id} {nav_date}")
        expected = nav["cash"] + nav["positions_value"]
        difference = expected - nav["nav"]
        return {
            "account_id": account_id,
            "nav_date": nav_date,
            "expected_nav": expected,
            "recorded_nav": nav["nav"],
            "difference": difference,
            "ok": abs(difference) <= self.NAV_TOLERANCE,
        }

    def list_accounts(self):
        return self.store.list_accounts()

    def get_account(self, account_id):
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        return account

    def confirm_shadow_order(self, fill: ManualFill):
        account = self.store.get_account(fill.account_id)
        if account is None:
            raise KeyError(fill.account_id)
        if account["account_type"] != AccountType.SHADOW.value:
            raise ValueError("manual fill is only allowed for shadow accounts")

        order = self.store.get_order(fill.order_id)
        if order is None:
            raise KeyError(fill.order_id)
        if order["account_id"] != fill.account_id:
            raise ValueError("order does not belong to account")
        if order["status"] != OrderStatus.PENDING.value:
            raise ValueError("only PENDING orders can be confirmed")
        if order["trade_date"] != fill.trade_date:
            raise ValueError("fill trade_date does not match order trade_date")

        action = order["action"]
        ticker = order["ticker"]
        shares = fill.actual_shares
        price = fill.actual_price
        commission = max(shares * price * self.COMMISSION_RATE, self.MIN_COMMISSION)

        # Use the latest state already recorded for trade_date (if any) so that
        # multiple shadow fills on the same day accumulate instead of overwriting.
        current_nav = self.store.get_nav(fill.account_id, fill.trade_date)
        if current_nav is not None:
            base_date = fill.trade_date
            cash = current_nav["cash"]
            base_positions = self.store.list_positions(fill.account_id, base_date)
        else:
            base_date = self.store.get_previous_nav_date(fill.account_id, fill.trade_date)
            prev_nav = self.store.get_nav(fill.account_id, base_date)
            if prev_nav is None:
                raise ValueError(f"missing previous NAV for {fill.account_id}")
            cash = prev_nav["cash"]
            base_positions = self.store.list_positions(fill.account_id, base_date)

        prev_positions = {p["ticker"]: dict(p) for p in base_positions}
        positions_value = 0.0

        if action == "BUY":
            cost = shares * price + commission
            if cash < cost:
                raise ValueError(f"insufficient cash: need {cost:.2f}, have {cash:.2f}")
            cash -= cost
            pos = prev_positions.get(ticker, {
                "shares": 0,
                "cost_price": price,
                "last_price": price,
            })
            old_shares = pos["shares"]
            total_shares = old_shares + shares
            old_cost = pos.get("cost_price", price) * old_shares
            new_cost = price * shares
            pos["shares"] = total_shares
            pos["cost_price"] = (old_cost + new_cost) / total_shares if total_shares > 0 else price
            pos["last_price"] = price
            prev_positions[ticker] = pos
        elif action in ("SELL", "STOP_LOSS"):
            available = prev_positions.get(ticker, {}).get("shares", 0)
            if shares > available:
                raise ValueError(f"oversell: want {shares}, have {available}")
            cash += shares * price - commission
            pos = prev_positions.get(ticker, {"shares": 0})
            pos["shares"] -= shares
            pos["last_price"] = price
            if pos["shares"] <= 0:
                prev_positions.pop(ticker, None)
            else:
                prev_positions[ticker] = pos
        else:
            raise ValueError(f"unknown action: {action}")

        position_rows = []
        for t, pos in prev_positions.items():
            if pos["shares"] <= 0:
                continue
            mv = pos["shares"] * pos["last_price"]
            positions_value += mv
            position_rows.append({
                "account_id": fill.account_id,
                "as_of_date": fill.trade_date,
                "ticker": t,
                "shares": pos["shares"],
                "cost_price": pos.get("cost_price", pos["last_price"]),
                "last_price": pos["last_price"],
                "market_value": mv,
            })

        nav = cash + positions_value
        nav_row = {
            "account_id": fill.account_id,
            "nav_date": fill.trade_date,
            "cash": cash,
            "positions_value": positions_value,
            "nav": nav,
            "data_date": fill.trade_date,
        }

        trade_id = f"trade-{fill.account_id}-{fill.trade_date}-{ticker}-{action}"
        trade_row = {
            "trade_id": trade_id,
            "dedupe_key": f"{fill.account_id}:{fill.trade_date}:{ticker}:{action}",
            "order_id": fill.order_id,
            "account_id": fill.account_id,
            "trade_date": fill.trade_date,
            "ticker": ticker,
            "action": action,
            "shares": shares,
            "price": price,
            "commission": commission,
            "source": "MANUAL",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

        self.store.apply_manual_fill(
            fill_row={
                "order_id": fill.order_id,
                "status": OrderStatus.FILLED.value,
                "reason": order.get("reason", ""),
            },
            trade_row=trade_row,
            position_rows=position_rows,
            nav_row=nav_row,
        )
        return self.store.get_order(fill.order_id)

    def reject_shadow_order(self, account_id: str, order_id: str, reason: str):
        if not reason or not reason.strip():
            raise ValueError("reason is required to reject an order")
        order = self._require_pending_shadow_order(account_id, order_id)
        self.store.update_order_status(
            order_id, OrderStatus.REJECTED.value, reason=reason
        )
        return self.store.get_order(order_id)

    def cancel_shadow_order(self, account_id: str, order_id: str, reason: str):
        if not reason or not reason.strip():
            raise ValueError("reason is required to cancel an order")
        order = self._require_pending_shadow_order(account_id, order_id)
        self.store.update_order_status(
            order_id, OrderStatus.CANCELLED.value, reason=reason
        )
        return self.store.get_order(order_id)

    def expire_shadow_orders(self, account_id: str, trade_date: str):
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        if account["account_type"] != AccountType.SHADOW.value:
            raise ValueError("expire only applies to shadow accounts")
        self.store.expire_pending_orders(account_id, before_trade_date=trade_date)
        return self.store.list_orders(account_id, status=OrderStatus.EXPIRED.value)

    def _require_pending_shadow_order(self, account_id: str, order_id: str):
        account = self.store.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        if account["account_type"] != AccountType.SHADOW.value:
            raise ValueError("manual actions are only allowed for shadow accounts")
        order = self.store.get_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order["account_id"] != account_id:
            raise ValueError("order does not belong to account")
        if order["status"] != OrderStatus.PENDING.value:
            raise ValueError("order is not PENDING")
        return order

    COMMISSION_RATE = 0.0003
    MIN_COMMISSION = 5.0

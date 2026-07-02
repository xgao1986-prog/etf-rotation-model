# src/paper_trading/ui_service.py — batch account creation and UI workflows.
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import pandas as pd

from .models import AccountCreate, AccountType, OpeningPosition, StartMode, config_hash


class PaperTradingUIService:
    """High-level workflows used by the virtual-account Streamlit page."""

    def __init__(self, service, runner):
        self.service = service
        self.runner = runner

    def create_comparison_accounts(
        self,
        preset_names: Sequence[str],
        presets: Mapping[str, Mapping[str, Any]],
        initial_capital: float,
        start_date: str,
        group_id: str | None = None,
    ) -> List[str]:
        """Create one comparison account per selected preset with shared capital/date."""
        if not preset_names:
            raise ValueError("at least one preset must be selected")
        if initial_capital <= 0:
            raise ValueError("initial capital must be positive")

        group_id = group_id or f"grp-{uuid.uuid4().hex[:8]}"
        created_ids: List[str] = []

        for preset_name in preset_names:
            preset = presets.get(preset_name)
            if preset is None:
                raise ValueError(f"preset not found: {preset_name}")

            account_id = self._make_account_id(
                AccountType.COMPARISON, preset_name, start_date, preset
            )
            if self.service.store.get_account(account_id):
                raise ValueError(f"account already exists: {account_id}")

            request = AccountCreate(
                account_id=account_id,
                name=f"{preset_name} {start_date}",
                account_type=AccountType.COMPARISON,
                strategy_name=preset_name,
                strategy_config=dict(preset),
                initial_capital=initial_capital,
                start_mode=StartMode.CASH,
                start_date=start_date,
                group_id=group_id,
            )
            self.service.create_account(request)
            created_ids.append(account_id)

        return created_ids

    def create_shadow_account(
        self,
        name: str,
        preset_name: str,
        preset: Mapping[str, Any],
        initial_capital: float,
        opening_cash: float,
        opening_positions: Iterable[OpeningPosition],
        start_date: str,
    ) -> str:
        """Create a shadow account from imported cash and positions.

        Raises ValueError if opening_cash + position value does not equal
        the user-supplied initial_capital (total opening NAV).
        """
        positions = tuple(opening_positions)
        positions_value = sum(p.market_value for p in positions)
        opening_nav = opening_cash + positions_value
        if abs(opening_nav - initial_capital) > self.service.NAV_TOLERANCE:
            raise ValueError(
                f"opening NAV mismatch: cash {opening_cash:.2f} + positions "
                f"{positions_value:.2f} = {opening_nav:.2f} != {initial_capital:.2f}"
            )
        if initial_capital <= 0:
            raise ValueError("initial capital must be positive")

        account_id = self._make_account_id(
            AccountType.SHADOW, preset_name, start_date, preset
        )
        if self.service.store.get_account(account_id):
            raise ValueError(f"account already exists: {account_id}")

        request = AccountCreate(
            account_id=account_id,
            name=name,
            account_type=AccountType.SHADOW,
            strategy_name=preset_name,
            strategy_config=dict(preset),
            initial_capital=initial_capital,
            start_mode=StartMode.IMPORTED,
            start_date=start_date,
            opening_cash=opening_cash,
            opening_positions=positions,
        )
        self.service.create_account(request)
        return account_id

    def list_account_summaries(self) -> List[Dict[str, Any]]:
        """Return a summary row for every account."""
        summaries = []
        for account in self.service.list_accounts():
            latest_nav = self._latest_nav(account["account_id"])
            summaries.append({
                "account_id": account["account_id"],
                "name": account["name"],
                "account_type": account["account_type"],
                "strategy_name": account["strategy_name"],
                "group_id": account["group_id"],
                "status": account["status"],
                "initial_capital": account["initial_capital"],
                "start_date": account["start_date"],
                "cash": latest_nav["cash"] if latest_nav else account["initial_capital"],
                "positions_value": latest_nav["positions_value"] if latest_nav else 0.0,
                "nav": latest_nav["nav"] if latest_nav else account["initial_capital"],
                "latest_nav_date": latest_nav["nav_date"] if latest_nav else None,
            })
        return summaries

    def run_accounts(
        self,
        account_ids: Sequence[str],
        trade_date: str,
        open_prices: Mapping[str, float],
        close_prices: Mapping[str, float],
        scores_df: pd.DataFrame,
    ) -> Dict[str, Dict[str, str]]:
        """Run a trading day for each selected account, isolating failures."""
        results: Dict[str, Dict[str, str]] = {"success": {}, "failure": {}}
        for account_id in account_ids:
            try:
                self.runner.run_daily(
                    account_id,
                    trade_date,
                    open_prices,
                    close_prices,
                    scores_df,
                )
                results["success"][account_id] = trade_date
            except Exception as exc:
                results["failure"][account_id] = str(exc)
        return results

    def list_pending_shadow_orders(self) -> List[Dict[str, Any]]:
        """Return all PENDING orders belonging to shadow accounts."""
        pending = []
        for account in self.service.list_accounts():
            if account["account_type"] != AccountType.SHADOW.value:
                continue
            pending.extend(
                self.service.store.list_orders(
                    account["account_id"], status="PENDING"
                )
            )
        return pending

    def _latest_nav(self, account_id: str):
        history = self.service.store.list_nav_history(account_id)
        return history[-1] if history else None

    def _make_account_id(
        self,
        account_type: AccountType,
        preset_name: str,
        start_date: str,
        preset: Mapping[str, Any],
    ) -> str:
        short_hash = config_hash(preset)[:12]
        safe_name = preset_name.replace(" ", "_").replace("/", "_")
        base = f"{account_type.value}-{safe_name}-{start_date}-{short_hash}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]

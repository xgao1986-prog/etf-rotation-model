#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/paper_account_admin.py — minimal virtual account administration command"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import AccountCreate, AccountType, StartMode
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore


def main():
    parser = argparse.ArgumentParser(description="Virtual paper account admin")
    parser.add_argument("--db", default="database/paper_trading.db", help="Database path")

    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create-cash", help="Create a cash-start account")
    create_parser.add_argument("--account-id", required=True)
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--strategy", required=True)
    create_parser.add_argument("--initial-capital", type=float, required=True)
    create_parser.add_argument("--start-date", required=True)
    create_parser.add_argument("--config-json", required=True)
    create_parser.add_argument("--account-type", default="COMPARISON", choices=["COMPARISON", "SHADOW"])
    create_parser.add_argument("--group-id", default=None)
    create_parser.add_argument("--end-date", default=None)

    sub.add_parser("list", help="List all accounts")

    inspect_parser = sub.add_parser("inspect", help="Inspect an account")
    inspect_parser.add_argument("--account-id", required=True)

    args = parser.parse_args()

    store = PaperTradingStore(args.db)
    service = PaperTradingService(store)

    if args.command == "create-cash":
        request = AccountCreate(
            account_id=args.account_id,
            name=args.name,
            account_type=AccountType(args.account_type),
            strategy_name=args.strategy,
            strategy_config=json.loads(args.config_json),
            initial_capital=args.initial_capital,
            start_mode=StartMode.CASH,
            start_date=args.start_date,
            end_date=args.end_date,
            group_id=args.group_id,
        )
        account = service.create_account(request)
        print(json.dumps(account, ensure_ascii=False, sort_keys=True))

    elif args.command == "list":
        accounts = service.list_accounts()
        for account in accounts:
            print(json.dumps(account, ensure_ascii=False, sort_keys=True))

    elif args.command == "inspect":
        account = service.get_account(args.account_id)
        nav = service.get_nav(args.account_id, account["start_date"])
        reconciliation = service.reconcile(args.account_id, account["start_date"])
        result = {
            "account": account,
            "opening_nav": nav,
            "reconciliation": reconciliation,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

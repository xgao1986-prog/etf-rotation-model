#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_account_admin.py — command-level smoke tests"""

import json
import os
import subprocess
import sys
import tempfile


SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "paper_account_admin.py"
)


def test_create_and_list_cash_account():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "paper.db")
        create = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--db",
                db_path,
                "create-cash",
                "--account-id",
                "acct-cli",
                "--name",
                "CLI B0.4",
                "--strategy",
                "B0.4",
                "--initial-capital",
                "1000000",
                "--start-date",
                "2026-06-29",
                "--config-json",
                '{"max_holdings":5}',
            ],
            capture_output=True,
            text=True,
        )
        assert create.returncode == 0, create.stderr
        listed = subprocess.run(
            [sys.executable, SCRIPT, "--db", db_path, "list"],
            capture_output=True,
            text=True,
        )
        assert listed.returncode == 0, listed.stderr
        assert "acct-cli" in listed.stdout
        assert "B0.4" in listed.stdout

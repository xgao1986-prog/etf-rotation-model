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
            # 账户 config 含中文 score_source：子进程按 PYTHONIOENCODING=utf-8
            # 输出，必须显式以 utf-8 解码（Windows 默认 GBK 会解码失败）
            encoding="utf-8",
        )
        assert create.returncode == 0, create.stderr
        listed = subprocess.run(
            [sys.executable, SCRIPT, "--db", db_path, "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert listed.returncode == 0, listed.stderr
        assert "acct-cli" in listed.stdout
        assert "B0.4" in listed.stdout

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iFinD 数据网关 —— 通过 subprocess 调用 Kimi iFinD 插件获取行情数据。

设计文档: docs/DATA_UPDATE_FIX_v2_DESIGN.md 第 3.1 节

调用链:
  本模块 -> python scripts/ifind_tool.py call --api-name ifind_get_price
         --params-json {"ticker": ..., "start_date": ..., "end_date": ...,
                        "file_path": ..., "adjust": ...}
         -> 插件写出 CSV -> 本模块读回并标准化

特性:
  - 自动按 3 只/组拆分（ifind_get_price 单次最多 3 ticker）
  - 单组超时、指数退避重试
  - 整体耗时预算：超预算后剩余组直接标记失败返回
  - 单组失败不影响其他组；任何异常不外抛裸异常，转为 failed 明细

环境配置（环境变量优先，默认路径兜底）:
  IFIND_PLUGIN_DIR  默认 C:\\...\\plugins\\managed\\ifind
  IFIND_PYTHON      默认 C:\\...\\Python310\\python.exe
"""

import json
import os
import subprocess
import tempfile
import time

import pandas as pd

DEFAULT_PLUGIN_DIR = os.path.join(
    os.environ.get("USERPROFILE", r"C:\Users\Gao Xiang"),
    "AppData", "Roaming", "kimi-desktop", "daimon-share", "daimon",
    "runtime", "kimi-code", "home", "plugins", "managed", "ifind",
)
DEFAULT_PYTHON = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Gao Xiang\AppData\Local"),
    "Programs", "Python", "Python310", "python.exe",
)

GROUP_SIZE = 3  # ifind_get_price 单次最多 3 只 ticker

# is_available() 结果缓存（进程内）
_availability_cache = None


def get_plugin_dir():
    return os.environ.get("IFIND_PLUGIN_DIR", DEFAULT_PLUGIN_DIR)


def get_python_path():
    return os.environ.get("IFIND_PYTHON", DEFAULT_PYTHON)


def get_tool_script():
    return os.path.join(get_plugin_dir(), "scripts", "ifind_tool.py")


def is_available(force=False):
    """检测 iFinD 网关是否可用（文件级检测，不做真实网络探测）。

    Parameters:
        force: 忽略缓存重新检测

    Returns:
        dict: {
            "available": bool,
            "message": str,
            "plugin_dir": str,
            "python": str,
            "tool_script": str,
        }
    """
    global _availability_cache
    if _availability_cache is not None and not force:
        return _availability_cache

    plugin_dir = get_plugin_dir()
    python = get_python_path()
    tool_script = get_tool_script()

    problems = []
    if not os.path.isdir(plugin_dir):
        problems.append(f"插件目录不存在: {plugin_dir}")
    if not os.path.isfile(tool_script):
        problems.append(f"工具脚本不存在: {tool_script}")
    if not os.path.isfile(python):
        problems.append(f"Python 解释器不存在: {python}")

    available = not problems
    _availability_cache = {
        "available": available,
        "message": ("iFinD 网关可用（文件级检测通过）" if available
                    else "iFinD 网关不可用: " + "；".join(problems)),
        "plugin_dir": plugin_dir,
        "python": python,
        "tool_script": tool_script,
    }
    return _availability_cache


def _run_command(cmd, timeout):
    """执行一次插件调用（抽成独立函数便于测试 mock）。

    Raises:
        subprocess.TimeoutExpired: 超时
        RuntimeError: 非零返回码
    """
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "")[-500:]
        stdout = (proc.stdout or "")[-500:]
        raise RuntimeError(
            f"ifind_tool.py 返回码 {proc.returncode}: {stderr or stdout or '无输出'}"
        )
    return proc


def _call_group(group, start_date, end_date, adjust, timeout):
    """调用一次 ifind_get_price（最多 3 只 ticker），返回标准化 DataFrame。

    Raises:
        任何失败都以异常形式抛出（超时/非零返回/CSV 缺列/空数据）
    """
    fd, csv_path = tempfile.mkstemp(prefix="ifind_gw_", suffix=".csv")
    os.close(fd)
    try:
        params = {
            "ticker": ",".join(group),
            "start_date": start_date,
            "end_date": end_date,
            "file_path": csv_path,
            "adjust": adjust,
        }
        cmd = [
            get_python_path(),
            get_tool_script(),
            "call",
            "--api-name", "ifind_get_price",
            "--params-json", json.dumps(params),
        ]
        # 传参使用 argv 列表（不走 shell），Windows 下 JSON 引号无需额外转义
        _run_command(cmd, timeout)

        if not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0:
            raise RuntimeError("插件未写出 CSV 文件或文件为空")

        try:
            df = pd.read_csv(csv_path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(csv_path, encoding="gbk")

        if df.empty:
            raise RuntimeError("插件返回空数据")

        # 标准化列名（thscode/time 为 iFinD 标准输出列名）
        rename_map = {}
        for col in df.columns:
            low = str(col).lower()
            if low == "thscode":
                rename_map[col] = "ticker"
            elif low == "time":
                rename_map[col] = "date"
        if rename_map:
            df = df.rename(columns=rename_map)

        required = ["ticker", "date", "open", "high", "low", "close", "volume"]
        missing_cols = [c for c in required if c not in df.columns]
        if missing_cols:
            raise RuntimeError(f"CSV 缺少必要列: {missing_cols}（实际列: {list(df.columns)}）")

        df = df[required].copy()
        # iFinD time 列为 'YYYYMMDD' 纯数字字符串；pd.to_datetime 对纯数字
        # 会按 epoch 解析（得到 1970-01-01），必须显式指定格式，并兼容
        # 已是 'YYYY-MM-DD' 的情形
        raw_dates = df["date"].astype(str).str.strip()
        parsed = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
        bad = parsed.isna()
        if bad.any():
            parsed.loc[bad] = pd.to_datetime(raw_dates.loc[bad], errors="coerce")
        df["date"] = parsed.dt.strftime("%Y-%m-%d")
        df = df.dropna(subset=["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df.sort_values(["ticker", "date"]).reset_index(drop=True)
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


def get_price(tickers, start_date, end_date, adjust="forward",
              timeout=60, max_retries=2, total_budget_seconds=1200):
    """获取多只标的的日频行情（forward 前复权）。

    Parameters:
        tickers: 标的代码列表（如 ["512480.SH", "000300.SH"]）
        start_date / end_date: 'YYYY-MM-DD'
        adjust: 'forward'（默认）/ 'backward' / 'none'
        timeout: 单组超时秒数
        max_retries: 单组失败后的重试次数（指数退避），总尝试次数 = max_retries + 1
        total_budget_seconds: 整体耗时预算；超预算后剩余组直接标记失败

    Returns:
        dict: {
            "df": 标准化 DataFrame（列: ticker,date,open,high,low,close,volume），
            "succeeded": 成功 ticker 列表,
            "failed": {ticker: 错误信息},
            "elapsed_seconds": float,
        }
        任何异常不外抛，单组失败不影响其他组。
    """
    result = {
        "df": pd.DataFrame(),
        "succeeded": [],
        "failed": {},
        "elapsed_seconds": 0.0,
    }
    t0 = time.time()

    tickers = [t for t in (tickers or []) if t]
    if not tickers:
        result["message"] = "无有效 ticker"
        return result

    groups = [tickers[i:i + GROUP_SIZE] for i in range(0, len(tickers), GROUP_SIZE)]
    frames = []

    for group in groups:
        # 整体耗时预算检查：超预算后剩余组不再执行
        if time.time() - t0 > total_budget_seconds:
            for t in group:
                result["failed"][t] = f"超出整体耗时预算（{total_budget_seconds:.0f}s），未执行"
            continue

        ok = False
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                df = _call_group(group, start_date, end_date, adjust, timeout)
                frames.append(df)
                result["succeeded"].extend(group)
                ok = True
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = min(2 ** attempt, 5)  # 指数退避: 1s, 2s, 4s...
                    time.sleep(wait)

        if not ok:
            err_msg = f"{type(last_error).__name__}: {last_error}"
            for t in group:
                result["failed"][t] = err_msg

    if frames:
        result["df"] = pd.concat(frames, ignore_index=True)

    result["elapsed_seconds"] = time.time() - t0
    return result

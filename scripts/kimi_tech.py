"""
KimiFinance iFind 技术指标模块
通过 Kimi Finance realtime_tech 接口获取 iFind 5分钟级别技术指标
"""

import ast
import json
import os
import sys
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 支持独立运行或被 monitor.py 导入
sys.path.insert(0, str(Path(__file__).parent))
from ifind_parser import parse_ifind_tech_data


def _load_kimi_cfg() -> dict:
    """从 ../config.json 或环境变量加载 KimiFinance 配置"""
    cfg_file = Path(__file__).parent.parent / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            cfg = {
                "api_key": data.get("kimiCodeAPIKey"),
                "base_url": data.get("kimiCodeBaseUrl", "https://api.kimi.com/coding/v1/tools"),
                "timeout": data.get("kimiCodeTimeout", 30)
            }
        except Exception:
            pass
    # 环境变量优先
    api_key = os.environ.get("kimiCodeAPIKey")
    if api_key:
        cfg["api_key"] = api_key
    return cfg


# 模块加载时读一次作为默认值
_DEFAULT_CFG = _load_kimi_cfg()


def _get_fresh_cfg() -> dict:
    """重新读取配置（解决缓存问题）"""
    return _load_kimi_cfg()


class TechnicalCalculator:
    """iFind 5分钟技术指标计算器（via KimiFinance）"""

    @staticmethod
    def fetch_from_kimi(ticker: str, time_str: str, kimi_cfg: dict = None) -> Optional[dict]:
        """
        从 KimiFinance 获取 iFind 技术指标

        Args:
            ticker:   股票代码，如 "000001.SZ"
            time_str: "YYYY-MM-DD HH:MM:SS"
            kimi_cfg: 配置字典（不传则用模块默认配置）
        """
        cfg = kimi_cfg or _get_fresh_cfg()
        api_key = cfg.get("api_key", "")
        if not api_key or api_key.startswith("YOUR_"):
            print("❌ kimiCodeAPIKey 未配置")
            return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OpenClaw/1.0",
        }
        payload = {
            "method": "get_stock_realtime_price",
            "params": {
                "ticker": ticker,
                "time": time_str,
                "type": "realtime_tech",
                "file_path": f"/tmp/ifind_{ticker.replace('.','_')}_{time_str.replace(' ','_').replace(':','-')}.json",
            },
        }

        try:
            resp = requests.post(
                cfg.get("base_url", "https://api.kimi.com/coding/v1/tools"),
                headers=headers,
                json=payload,
                timeout=cfg.get("timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("is_success"):
                err = data.get("result", {}).get("user", [{}])[0].get("text", "未知错误")
                print(f"❌ iFind API 错误: {err}")
                return None

            result_text = data.get("result", {}).get("user", [{}])[0].get("text", "")
            result_json = json.loads(result_text)
            is_success  = result_json.get("is_success", "")

            # 解析两种响应格式
            if isinstance(is_success, str) and is_success.startswith("ifind"):
                if is_success.startswith("ifind, {"):
                    json_str = is_success[7:]  # 去掉 "ifind, "
                    inner    = ast.literal_eval(json_str)
                    data_str = inner.get("data_str", "")
                    raw_data = ast.literal_eval(data_str) if data_str else []
                else:
                    data_str = result_json.get("data_str", "")
                    if not data_str:
                        print("⚠️ iFind 返回数据为空")
                        return None
                    try:
                        raw_data = ast.literal_eval(data_str)
                    except Exception:
                        raw_data = json.loads(data_str)
            else:
                if isinstance(is_success, str) and "Response data is empty" in is_success:
                    print("⚠️ iFind 返回空数据（非交易日或非交易时间）")
                else:
                    print(f"⚠️ iFind 返回: {is_success}")
                return None

            if not raw_data or len(raw_data) < 3:
                print(f"⚠️ iFind 数据不足，期望3行，实际{len(raw_data)}行")
                return None

            result = parse_ifind_tech_data(raw_data)
            
            # 检查是否所有技术指标都为 None（如 ETF 的情况）
            tech_fields = ["MA5", "MACD_DIFF", "KDJ_K", "RSI6"]
            has_valid_tech = any(result.get(f) is not None for f in tech_fields)
            
            if not has_valid_tech:
                print(f"  ⚠️ {ticker} 无技术指标数据（可能为 ETF），尝试获取基础行情...")
                # 对于 ETF，返回空结果但标记为已处理
                # 价格数据会从同花顺接口获取
                return {"_no_tech": True, "code": ticker, "time": time_str}
            
            return result

        except requests.exceptions.Timeout:
            print(f"❌ KimiFinance 请求超时: {ticker}")
        except requests.exceptions.RequestException as e:
            print(f"❌ KimiFinance 请求错误: {e}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析错误: {e}")
        except Exception as e:
            print(f"❌ 未知错误: {e}")
        return None

    # ------------------------------------------------------------------
    # 时间工具
    # ------------------------------------------------------------------

    @staticmethod
    def _aligned_time_str(mock_now=None) -> str:
        """返回当前时间往前推一个 5 分钟对齐点（确保数据已生成）"""
        now = mock_now or datetime.now()
        minute = (now.minute // 5) * 5 - 5
        if minute < 0:
            minute += 60
            hour = now.hour - 1
            if hour < 0:
                yesterday = now - timedelta(days=1)
                return yesterday.strftime("%Y-%m-%d 14:55:00")
            return now.strftime(f"%Y-%m-%d {hour:02d}:{minute:02d}:00")
        return now.strftime(f"%Y-%m-%d %H:{minute:02d}:00")

    @staticmethod
    def _is_near_trading_time(now=None) -> bool:
        """是否在交易时间附近（含收盘后15分钟缓冲）"""
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        tv = now.hour * 100 + now.minute
        return (930 <= tv <= 1145) or (1300 <= tv <= 1515)

    @staticmethod
    def _last_trading_time_str(now=None) -> str:
        """盘外时返回最近交易时段末尾时间点"""
        now = now or datetime.now()
        tv  = now.hour * 100 + now.minute
        if now.weekday() < 5 and tv > 1515:
            return now.strftime("%Y-%m-%d 14:55:00")
        if now.weekday() < 5 and 1146 <= tv < 1300:
            return now.strftime("%Y-%m-%d 11:25:00")
        target = now - timedelta(days=1)
        while target.weekday() >= 5:
            target -= timedelta(days=1)
        return target.strftime("%Y-%m-%d 14:55:00")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    @classmethod
    def calculate_all(
        cls,
        code: str,
        time_str: str = None,
        mock_now=None,
        kimi_cfg: dict = None,
    ) -> dict:
        """
        获取指定股票的 iFind 5分钟技术指标。

        若首选时间获取失败，自动往前尝试最多 30 个 5 分钟间隔。
        盘外时自动定位到最近交易时段末尾，结果携带 market_closed=True。

        Args:
            code:     股票代码，如 "000001.SZ"
            time_str: 手动指定时间；None 则根据当前时间自动计算
            mock_now: 测试用模拟时间（time_str 为 None 时生效）
            kimi_cfg: KimiFinance 配置字典；None 则用模块默认
        """
        is_market_closed = False
        effective_now = mock_now or datetime.now()
        if time_str is None:
            if not cls._is_near_trading_time(now=effective_now):
                is_market_closed = True

        if time_str is None:
            time_str = (
                cls._last_trading_time_str(now=effective_now)
                if is_market_closed
                else cls._aligned_time_str(mock_now=mock_now)
            )

        result = cls.fetch_from_kimi(code, time_str, kimi_cfg=kimi_cfg)
        used_time = time_str

        # 失败则往前最多回退 30 个 5 分钟
        if result is None:
            base_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            for i in range(1, 31):
                t = base_dt - timedelta(minutes=i * 5)
                tv = t.hour * 100 + t.minute
                if not (930 <= tv <= 1130 or 1300 <= tv <= 1500):
                    continue
                ts = t.strftime("%Y-%m-%d %H:%M:%S")
                print(f"  ⏱ 尝试 {ts}...")
                result = cls.fetch_from_kimi(code, ts, kimi_cfg=kimi_cfg)
                if result is not None:
                    used_time = ts
                    print(f"  ✅ 获取成功（时间: {used_time}）")
                    break

        # 全部失败，返回空模板
        if result is None:
            return {
                "code": code, "time": time_str, "interval": "5min",
                "error": "无法获取数据（已尝试多个时间点）",
                "MA5": None, "MA10": None, "MA20": None,
                "MACD_DIFF": None, "MACD_DEA": None, "MACD_BAR": None,
                "KDJ_K": None, "KDJ_D": None, "KDJ_J": None,
                "RSI6": None, "CCI14": None, "ROC": None,
                "LB5": None, "OBV": None, "BBI": None, "ATR14": None,
                "EXPMA5": None, "BOLL_UPPER": None, "BOLL_MID": None, "BOLL_LOWER": None,
            }

        result["code"] = code
        result["time"] = used_time
        if is_market_closed:
            result["market_closed"] = True
        return result


if __name__ == "__main__":
    result = TechnicalCalculator.calculate_all("000001.SZ")
    print(json.dumps(result, ensure_ascii=False, indent=2))

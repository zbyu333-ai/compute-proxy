#!/usr/bin/env python3
"""AI 算力指挥中心 — 代理层（第一阶段）

作用：拿着密钥定时调各家接口，吐一个 JSON 给静态 Space 读。
Space 里只放 DATA_URL，不放任何密钥。

- OpenRouter：已验证。GET https://openrouter.ai/api/v1/key 返回
  free_model_daily_requests {used, limit, remaining}，每日 UTC 午夜重置。
  未购满 $10 每日 50 次，购满 $10 后 1,000 次，都是 20 次/分钟。
- Kaggle：走官方 MCP https://www.kaggle.com/mcp，工具 get_accelerator_quota。
  MCP 传输细节尚未用真实 token 实测，失败时自动回落到 manual.json 的手动值，
  并在 note 里写明，不会让整条接口挂掉。
- Colab / Cloady / Sakupa / Simular：无公开剩余额度接口，读 manual.json 的手工值。

环境变量：
  OPENROUTER_API_KEY  OpenRouter 的 API key（sk-or-...）
  KAGGLE_API_TOKEN    Kaggle 的 API token（kaggle.com/settings/api 生成）
  PORT                监听端口，默认 8080

接口：
  GET /api/resources.json  -> {"updated_at": ..., "resources": [...6 条...]}
  GET /health             -> {"ok": true}

只用标准库，不用 pip 装任何东西。python3 server.py 即可跑。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_PATH = os.path.join(BASE_DIR, "manual.json")
CACHE_TTL = 300  # 5 分钟

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
KAGGLE_TOKEN = os.environ.get("KAGGLE_API_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

_cache = {"at": 0, "data": None}
_lock = threading.Lock()


def load_manual():
    with open(MANUAL_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------- OpenRouter ----------------

def fetch_openrouter():
    """返回 (patch_dict, error_str)，patch 用于覆盖 manual.json 里 openrouter 的数值字段。"""
    if not OPENROUTER_KEY:
        return None, "未配置 OPENROUTER_API_KEY"
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
    except Exception as e:  # noqa: BLE001 - 任何失败都回落，不让接口挂掉
        return None, f"请求失败: {e}"
    data = payload.get("data", payload) or {}
    fmr = data.get("free_model_daily_requests") or {}
    used = fmr.get("used")
    limit = fmr.get("limit")
    remaining = fmr.get("remaining")
    if used is None or limit is None:
        return None, "返回中没有 free_model_daily_requests"
    return {
        "quotaLabel": "今日免费请求",
        "quotaRemain": remaining if remaining is not None else max(limit - used, 0),
        "quotaTotal": limit,
        "usedPct": round(used / limit * 100) if limit else 0,
        "note": "实时 · 每日 UTC 午夜重置",
        "source": "live",
    }, None


# ---------------- Kaggle MCP ----------------

def _mcp_post(url, headers, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", "replace")
        return body, resp.headers.get("mcp-session-id")


def _parse_mcp_body(body):
    """MCP 可能回纯 JSON，也可能回 SSE（data: {...} 行），两种都解析。"""
    for line in body.strip().splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def _parse_duration(value):
    """Kaggle 配额是 Go 风格时长字符串，如 '108000s' -> 108000.0 秒；数字直接返回；其它返回 None。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*s\s*$", value)
        if m:
            return float(m.group(1))
    return None


def _find_numbers(obj, keys):
    """在嵌套结构里找名字含指定关键词的数值。"""
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (int, float)) and any(w in lk for w in keys):
                found[lk] = v
            found.update(_find_numbers(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_find_numbers(item, keys))
    return found


def fetch_kaggle():
    """返回 (patch_dict, error_str)。MCP 细节未实测，失败即回落。"""
    if not KAGGLE_TOKEN:
        return None, "未配置 KAGGLE_API_TOKEN"
    url = "https://www.kaggle.com/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {KAGGLE_TOKEN}",
    }
    try:
        body, session = _mcp_post(url, headers, {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "compute-proxy", "version": "0.1"}},
        })
        if session:
            headers["mcp-session-id"] = session
        try:
            _mcp_post(url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:  # noqa: BLE001 - 通知失败不致命
            pass
        body, _ = _mcp_post(url, headers, {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
            "params": {"name": "get_accelerator_quota", "arguments": {"request": {}}},
        })
        payload = _parse_mcp_body(body)
        if not payload:
            return None, "无法解析 MCP 返回"
        result = payload.get("result", {}) or {}
        text = ""
        for item in result.get("content", []) or []:
            if isinstance(item, dict) and item.get("text"):
                text += item["text"]
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = {"raw": text}
        remaining = None
        total = 30  # Kaggle 免费周额度 30 小时
        # 首选：官方返回的 gpu_quota（时长字符串，如 '108000s' = 30 小时）
        gpu = parsed.get("gpu_quota") if isinstance(parsed, dict) else None
        if isinstance(gpu, dict):
            total_s = _parse_duration(gpu.get("total_time_allowed"))
            used_s = _parse_duration(gpu.get("time_used"))
            if total_s is not None and used_s is not None:
                total = round(total_s / 3600, 2)
                remaining = round(max(total_s - used_s, 0) / 3600, 2)
        if remaining is None:
            # 兜底：旧的数值字段搜索
            nums = _find_numbers(parsed, ["remain", "quota", "hour", "limit", "used", "total"])
            for k in ("remaining_hours", "gpu_hours_remaining", "remaining", "quota_remaining"):
                if k in nums:
                    remaining = nums[k]
                    break
            for k in ("total_hours", "weekly_limit", "quota_total", "limit", "total"):
                if k in nums:
                    total = nums[k]
                    break
        if remaining is None:
            return None, f"配额返回无法解析: {text[:200]}"
        used = max(total - remaining, 0)
        return {
            "quotaLabel": "每周 GPU 小时",
            "quotaRemain": remaining,
            "quotaTotal": total,
            "usedPct": round(used / total * 100) if total else 0,
            "note": "实时 · 官方 MCP get_accelerator_quota",
            "source": "live",
        }, None
    except Exception as e:  # noqa: BLE001
        return None, f"MCP 调用失败: {e}"


# ---------------- 合并 ----------------

def build_resources():
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["data"]
        manual = load_manual()
        resources = {r["id"]: dict(r) for r in manual["resources"]}

        patch, err = fetch_openrouter()
        if patch:
            resources["openrouter"].update(patch)
        elif "openrouter" in resources:
            resources["openrouter"]["note"] = f"待接线：{err}"

        patch, err = fetch_kaggle()
        if patch:
            resources["kaggle"].update(patch)
        elif "kaggle" in resources:
            resources["kaggle"]["note"] = f"待接线：{err}"

        out = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "resources": [resources[r["id"]] for r in manual["resources"]],
        }
        _cache["at"] = time.time()
        _cache["data"] = out
        return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点，只打关键行
        print(f"{self.client_address[0]} {self.command} {self.path}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802 - 供浏览器跨域预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/api/resources.json":
            try:
                self._send_json(build_resources())
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
        elif self.path == "/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": True, "usage": "GET /api/resources.json"})


if __name__ == "__main__":
    print(f"compute-proxy listening on :{PORT}  (manual.json: {MANUAL_PATH})")
    print("OpenRouter key:", "已配置" if OPENROUTER_KEY else "未配置")
    print("Kaggle token:", "已配置" if KAGGLE_TOKEN else "未配置")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

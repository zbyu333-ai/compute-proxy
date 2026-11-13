#!/usr/bin/env python3
"""AI 算力指挥中心 — 代理层（第一阶段额度 + 第二阶段真下发）

作用：拿着密钥调各家接口，吐一个 JSON 给 Space 读；并接受 Space 的
派活请求，真的去调模型接口把任务跑了，把结果返回。
Space 里只放 DATA_URL，不放任何密钥。

- OpenRouter：已验证。GET https://openrouter.ai/api/v1/key 返回
  free_model_daily_requests {used, limit, remaining}，每日 UTC 午夜重置。
  未购满 $10 每日 50 次，购满 $10 后 1,000 次，都是 20 次/分钟。
- Kaggle：走官方 MCP https://www.kaggle.com/mcp，工具 get_accelerator_quota。
  MCP 传输细节尚未用真实 token 实测，失败时自动回落到 manual.json 的手动值，
  并在 note 里写明，不会让整条接口挂掉。
- Groq：无专用剩余额度接口。调 GET https://api.groq.com/openai/v1/models
  （轻量列表接口，不耗 completion 额度），读响应头 x-ratelimit-*：
  x-ratelimit-remaining-requests 即当日剩余请求数（RPD），
  x-ratelimit-limit-requests 即当日上限。读不到头时回落到 manual.json。
- SambaNova：OpenAI 兼容接口，POST https://api.sambanova.ai/v1/chat/completions。
  免费档模型用 Meta-Llama-3.3-70B-Instruct（2026-11-12 实测 /v1/models 在列，
  max_completion_tokens 3072，本代理 max_tokens 2048 在限内）。无公开剩余额度
  接口，额度只做手工登记。真下发可用。
- Gemini：API key 查不到剩余额度。剩余额度只在 AI Studio / Service Usage
  API（需 GCP 服务账号 OAuth，不只是 API key）里有，所以 Gemini 额度目前只做
  手工登记，不接实时。但真下发可以：调 generateContent 接口只要 API key 就行。
- Colab / Cloady / Sakupa / Simular：无公开剩余额度接口，读 manual.json 的手工值。
- Mistral：免费档不给 API key（须付费升级），2026-11-12 用户决定不付费、跳过，
  故不接真下发。
- NVIDIA NIM：账号已注册，但生成 API key 前须手机验证；2026-11-12 验证时账号
  被临时封禁 12 小时，2026-11-13 04:00 CST 后才能重试。未接。
- Cloudflare Workers AI：2026-11-13 用户已在自己浏览器登进 dash.cloudflare.com，
  正在建 token（自定义权限，“人工智能与机器学习”分组 17 项全选）。已接真下发：
  OpenAI 兼容端点 POST /client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions，
  模型 @cf/meta/llama-3.1-8b-instruct。注意光有 token 不够，还要 Account ID
  （控制台右侧栏复制），否则调不通。
- NEX（复旦 MOSS，platform.moss.cn）：OpenAI 兼容形态，POST
  https://api.mosi.cn/v1/chat/completions（注意域名是 api.mosi.cn）。
  模型 nex-n2.6，基于 GLM-5.3-Flash 后训练，全模态。限时免费、调用 0 积分，
  但无定量额度、无免费截止日、无 RPM/TPM 与速度承诺（评论实测约 9 token/s），
  协议保留随时调价/改额度/下线模型的权利；商用须另行签协议。真下发可用，
  定位为低成本试点/降级通道，不当主力。注册免实名、免绑卡（邮箱或手机号
  验证码即可）。

环境变量：
  OPENROUTER_API_KEY  OpenRouter 的 API key（sk-or-...）
  KAGGLE_API_TOKEN    Kaggle 的 API token（kaggle.com/settings/api 生成）
  GROQ_API_KEY        Groq 的 API key（console.groq.com 生成，gsk-...）
  GEMINI_API_KEY      Gemini 的 API key（aistudio.google.com 生成）
  SAMBANOVA_API_KEY   SambaNova 的 API key（cloud.sambanova.ai/apis 生成）
  MOSS_API_KEY        MOSS/NEX 的 API key（platform.moss.cn 控制台生成）
  CLOUDFLARE_API_TOKEN  Cloudflare API token（Workers AI 权限，dash.cloudflare.com 建）
  CLOUDFLARE_ACCOUNT_ID Cloudflare Account ID（控制台右侧栏复制，token 之外必填）
  PORT                监听端口，默认 8080

接口：
  GET /api/resources.json  -> {"updated_at": ..., "resources": [...8 条...]}
  GET /health             -> {"ok": true}
  POST /api/execute       -> {"provider","prompt","model"?} -> {"ok","result"}
    真下发：调模型接口跑任务。provider 限
    openrouter/groq/gemini/sambanova/nex/cloudflare；
    prompt 限 4000 字；每分钟最多 10 次（防刷额度）。

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
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
SAMBANOVA_KEY = os.environ.get("SAMBANOVA_API_KEY", "")
MOSS_KEY = os.environ.get("MOSS_API_KEY", "")
CLOUDFLARE_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
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


# ---------------- Groq ----------------

def fetch_groq():
    """返回 (patch_dict, error_str)。调 /openai/v1/models 读 x-ratelimit 响应头。

    x-ratelimit-remaining-requests = 当日剩余请求数（RPD），
    x-ratelimit-limit-requests = 当日上限。/models 是轻量列表接口，
    不耗 completion 额度。读不到头时回落，不让接口挂掉。
    """
    if not GROQ_KEY:
        return None, "未配置 GROQ_API_KEY"
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            headers = resp.headers
    except Exception as e:  # noqa: BLE001 - 任何失败都回落，不让接口挂掉
        return None, f"请求失败: {e}"
    try:
        limit = int(float(headers.get("x-ratelimit-limit-requests")))
        remaining = int(float(headers.get("x-ratelimit-remaining-requests")))
    except (TypeError, ValueError):
        return None, "响应头里没有 x-ratelimit-remaining-requests"
    if limit is None or remaining is None:
        return None, "响应头里没有 x-ratelimit-remaining-requests"
    used = max(limit - remaining, 0)
    return {
        "quotaLabel": "今日请求",
        "quotaRemain": remaining,
        "quotaTotal": limit,
        "usedPct": round(used / limit * 100) if limit else 0,
        "note": "实时 · 响应头 x-ratelimit（RPD）",
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


# ---------------- 真下发（第二阶段） ----------------

EXEC_MAX_PROMPT = 4000
EXEC_PER_MIN = 10
EXEC_TIMEOUT = 60

_exec_times = []
_exec_lock = threading.Lock()


def _exec_allowed():
    """每分钟最多 EXEC_PER_MIN 次真下发，防刷额度。返回 True 表示放行。"""
    now = time.time()
    with _exec_lock:
        _exec_times[:] = [t for t in _exec_times if now - t < 60]
        if len(_exec_times) >= EXEC_PER_MIN:
            return False
        _exec_times.append(now)
        return True


def _post_json(url, headers, payload, timeout=EXEC_TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _chat_completions(base_url, key, model, prompt):
    """OpenAI 兼容的 chat/completions：OpenRouter、Groq 共用。返回结果文本。"""
    payload = _post_json(
        base_url + "/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model,
         "messages": [{"role": "user", "content": prompt}],
         "max_tokens": 2048},
    )
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError(f"接口未返回 choices：{str(payload)[:200]}")
    return (choices[0].get("message") or {}).get("content") or ""


def _cloudflare_run(account_id, token, model, prompt):
    """Cloudflare Workers AI 原生接口：POST /ai/run/{model}。返回结果文本。"""
    payload = _post_json(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        {"messages": [{"role": "user", "content": prompt}]},
    )
    if not payload.get("success", False):
        errors = payload.get("errors") or []
        raise ValueError(f"Cloudflare 返回失败：{str(errors)[:200]}")
    result = payload.get("result") or {}
    if isinstance(result, dict):
        return result.get("response") or ""
    return str(result)


def _gemini_generate(model, prompt):
    """Gemini generateContent：只要 API key。返回结果文本。"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_KEY}"
    )
    payload = _post_json(
        url, {"Content-Type": "application/json"},
        {"contents": [{"parts": [{"text": prompt}]}]},
    )
    cands = payload.get("candidates") or []
    if not cands:
        raise ValueError(f"接口未返回 candidates：{str(payload)[:200]}")
    parts = ((cands[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def execute_task(provider, prompt, model=None):
    """真的调模型接口跑一个任务。返回 (result_dict, error_str)。"""
    provider = (provider or "").strip().lower()
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "prompt 为空"
    if len(prompt) > EXEC_MAX_PROMPT:
        return None, f"prompt 超过 {EXEC_MAX_PROMPT} 字"
    if provider == "openrouter":
        if not OPENROUTER_KEY:
            return None, "未配置 OPENROUTER_API_KEY"
        use_model = model or "nvidia/nemotron-3.5-lightning:free"
        try:
            text = _chat_completions(
                "https://openrouter.ai/api/v1", OPENROUTER_KEY, use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"OpenRouter 调用失败: {e}"
        return {"provider": "openrouter", "model": use_model, "result": text}, None
    if provider == "groq":
        if not GROQ_KEY:
            return None, "未配置 GROQ_API_KEY"
        use_model = model or "openai/gpt-oss-120b"
        try:
            text = _chat_completions(
                "https://api.groq.com/openai/v1", GROQ_KEY, use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"Groq 调用失败: {e}"
        return {"provider": "groq", "model": use_model, "result": text}, None
    if provider == "gemini":
        if not GEMINI_KEY:
            return None, "未配置 GEMINI_API_KEY"
        use_model = model or "gemini-3.5-flash-lite"
        try:
            text = _gemini_generate(use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"Gemini 调用失败: {e}"
        return {"provider": "gemini", "model": use_model, "result": text}, None
    if provider == "sambanova":
        if not SAMBANOVA_KEY:
            return None, "未配置 SAMBANOVA_API_KEY"
        use_model = model or "Meta-Llama-3.3-70B-Instruct"
        try:
            text = _chat_completions(
                "https://api.sambanova.ai/v1", SAMBANOVA_KEY, use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"SambaNova 调用失败: {e}"
        return {"provider": "sambanova", "model": use_model, "result": text}, None
    if provider == "nex":
        if not MOSS_KEY:
            return None, "未配置 MOSS_API_KEY"
        use_model = model or "nex-n2.6"
        try:
            text = _chat_completions(
                "https://api.mosi.cn/v1", MOSS_KEY, use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"NEX 调用失败: {e}"
        return {"provider": "nex", "model": use_model, "result": text}, None
    if provider == "cloudflare":
        if not CLOUDFLARE_TOKEN:
            return None, "未配置 CLOUDFLARE_API_TOKEN"
        if not CLOUDFLARE_ACCOUNT_ID:
            return None, "未配置 CLOUDFLARE_ACCOUNT_ID（在 Cloudflare 控制台右侧栏复制 Account ID，填进 Cloady 环境变量）"
        use_model = model or "@cf/meta/llama-3.1-8b-instruct"
        try:
            text = _cloudflare_run(
                CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_TOKEN, use_model, prompt)
        except Exception as e:  # noqa: BLE001
            return None, f"Cloudflare 调用失败: {e}"
        return {"provider": "cloudflare", "model": use_model, "result": text}, None
    return None, f"不支持的 provider：{provider}（只支持 openrouter/groq/gemini/sambanova/nex/cloudflare）"


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

        patch, err = fetch_groq()
        if patch:
            resources["groq"].update(patch)
        elif "groq" in resources:
            resources["groq"]["note"] = f"待接线：{err}"

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

    def do_POST(self):  # noqa: N802
        if self.path != "/api/execute":
            self._send_json({"ok": False, "error": "unknown path"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._send_json({"ok": False, "error": "请求体为空或过大"}, status=400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
            return
        if not _exec_allowed():
            self._send_json(
                {"ok": False, "error": "真下发每分钟最多 10 次，稍后再试"}, status=429)
            return
        result, err = execute_task(
            body.get("provider"), body.get("prompt"), body.get("model"))
        if err:
            self._send_json({"ok": False, "error": err}, status=502)
            return
        self._send_json({"ok": True, **result})


if __name__ == "__main__":
    print(f"compute-proxy listening on :{PORT}  (manual.json: {MANUAL_PATH})")
    print("OpenRouter key:", "已配置" if OPENROUTER_KEY else "未配置")
    print("Kaggle token:", "已配置" if KAGGLE_TOKEN else "未配置")
    print("Groq key:", "已配置" if GROQ_KEY else "未配置")
    print("Gemini key:", "已配置" if GEMINI_KEY else "未配置")
    print("SambaNova key:", "已配置" if SAMBANOVA_KEY else "未配置")
    print("MOSS/NEX key:", "已配置" if MOSS_KEY else "未配置")
    print("Cloudflare token:", "已配置" if CLOUDFLARE_TOKEN else "未配置")
    print("Cloudflare account:", "已配置" if CLOUDFLARE_ACCOUNT_ID else "未配置")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# AI 算力指挥中心 — 代理层

小代理，拿着密钥调各家接口，吐一个 JSON 给静态 Space 读。
Space 里只放 `DATA_URL`，不放任何密钥。

## 文件

- `server.py` — 服务本身，只用 Python 标准库，不用 pip 装任何东西
- `manual.json` — 手工值：Colab / Cloady / Sakupa / Simular 四格，以及 OpenRouter、Kaggle、Groq 在没配密钥时的回落值；Gemini、NEX 目前只做手工登记
- `Dockerfile` — 放容器里跑用的镜像

## 本地跑

```bash
cd proxy
OPENROUTER_API_KEY=sk-or-... KAGGLE_API_TOKEN=... GROQ_API_KEY=gsk-... GEMINI_API_KEY=... SAMBANOVA_API_KEY=... MOSS_API_KEY=... python3 server.py
# 打开 http://localhost:8080/api/resources.json 看一眼
# 健康检查：http://localhost:8080/health
```

不配密钥也能跑：OpenRouter、Kaggle、Groq 三格显示 `manual.json` 里的待接线值，
配了密钥后自动变实时值（5 分钟缓存一次）。

## 密钥从哪来

- **OpenRouter**：登录 openrouter.ai → Keys → 新建一个 key（`sk-or-...` 开头）
- **Kaggle**：Settings → API Tokens → Generate New Token，得到以 `KGAT` 开头的 MCP 专用 token（不是 kaggle.json 里的 Legacy API Key，那个会被 MCP 拒绝为 Unauthenticated）。`get_accelerator_quota` 调用时 arguments 必须是 `{"request": {}}`，返回里配额是 Go 时长字符串（如 `total_time_allowed: "108000s"` = 30 小时），已做解析。2026-11-11 用真实 KGAT token 本地实测通过：GPU 30 小时、TPU 20 小时、已用 0。
- **Groq**：登录 console.groq.com → API Keys → 新建一个 key（`gsk-...` 开头）。代理调 `GET /openai/v1/models` 读响应头 `x-ratelimit-remaining-requests`（当日剩余请求数），不耗 completion 额度。
- **Gemini**：API key 查不到剩余额度（剩余额度只在 AI Studio / Service Usage API 里，需 GCP 服务账号 OAuth，不只是 API key），所以 Gemini 目前只做手工登记，不接实时；免费额度在 `manual.json` 里按 Flash-Lite 每日 500 次登记，剩余额度请按实际用量手动修改。
- **SambaNova**：登录 cloud.sambanova.ai → APIs（API Keys）→ 新建一把 key（默认名 Default Key，有效期 90 天，到期需轮换重新生成）。接口是 OpenAI 兼容的 `https://api.sambanova.ai/v1/chat/completions`，默认模型 `Meta-Llama-3.3-70B-Instruct`（2026-11-12 实测在 `/v1/models` 在列；max_completion_tokens 3072，本代理 max_tokens 2048 在限内）。无公开剩余额度接口，额度只做手工登记；真下发可用。注意：key 生成时只完整显示一次（页面此后只显示掩码），须立即粘贴保存到安全处，丢失只能在页面 Rotate 重新生成。
- **NEX（复旦 MOSS）**：登录 platform.moss.cn → 控制台 API Keys → 新建一把 key。接口形态类 OpenAI：`POST https://api.mosi.cn/v1/chat/completions`（注意域名是 api.mosi.cn），默认模型 `nex-n2.6`（基于 GLM-5.3-Flash 后训练，全模态）。限时免费、调用 0 积分，但无定量额度、无免费截止日、无 RPM/TPM 与速度承诺（评论实测约 9 token/s）；协议保留随时调价/改额度/下线模型的权利，商用须另行签协议。额度只做手工登记；真下发可用，定位为低成本试点/降级通道，不当主力。注册免实名、免绑卡（邮箱或手机号验证码即可）。

密钥只放进部署环境的变量里，不写进 Space、不写进聊天记录、不提交到任何公开位置。

## 放容器里跑（默认方案：Cloady 容器）

```bash
docker build -t compute-proxy .
docker run -d --name compute-proxy -p 8080:8080 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e KAGGLE_API_TOKEN=... \
  -e GROQ_API_KEY=gsk-... \
  -e GEMINI_API_KEY=... \
  -e SAMBANOVA_API_KEY=... \
  -e MOSS_API_KEY=... \
  compute-proxy
```

前提：Cloady 免费档 11 月 16 日前的自查结果。
如果 16 号后免费档没了，代理不能再放 Cloady 免费容器上，另选落点。

## 真下发（第二阶段）：POST /api/execute

Space 派活时不再只记账，而是真的调模型接口跑任务：

```bash
curl -X POST https://你的代理地址/api/execute \
  -H 'Content-Type: application/json' \
  -d '{"provider":"groq","prompt":"用一句话介绍量子计算"}'
# -> {"ok":true,"provider":"groq","model":"openai/gpt-oss-120b","result":"..."}
```

- `provider` 只支持 `openrouter` / `groq` / `gemini` / `sambanova` / `nex`；`prompt` 限 4000 字；`model` 可省略（默认 OpenRouter 用 google/gemma-4-31b-it:free、Groq 用 gpt-oss-120b、Gemini 用 gemini-2.5-flash、SambaNova 用 Meta-Llama-3.3-70B-Instruct、NEX 用 nex-n2.6）。
- 每分钟最多 10 次真下发（防刷额度），超了返回 429。
- 需要的 key：OpenRouter、Groq、Gemini、SambaNova、NEX 的 key 都要配进部署环境变量（`OPENROUTER_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` / `SAMBANOVA_API_KEY` / `MOSS_API_KEY`）。没配 key 的那家会返回 502 报错，不会把代理搞挂。
- 注意：这个接口会消耗各家的免费额度，代理地址不要公开扩散。
- 未接真下发的工位：Mistral（免费档不给 key，用户不付费）、NVIDIA NIM（账号被临时封禁 12 小时，2026-11-13 04:00 CST 后重试手机验证拿 key）、Cloudflare Workers AI（云浏览器登录页 Turnstile 验证失败，未拿到 token）。

## Space 怎么接

Space 页面顶部有一个 `DATA_URL` 常量（默认空 = 用示例数据）。
代理跑起来后，把它改成代理的地址，例如：

```
https://你的代理地址/api/resources.json
```

Space 拉到数据就显示实时值并标注「实时」，拉不到回落到示例数据并标注「示例数据」。
`manual.json` 里改手工值后，代理 5 分钟内生效（缓存过期即刷新）。

## 已知局限

- Kaggle MCP 传输细节未实测：第一次部署务必看日志，确认配额拉取成功。
- Colab 没有公开剩余额度接口，只能手动登记。
- Gemini 的 API key 查不到剩余额度，只能手工登记（见上）。
- 「指派任务」真下发是第二阶段：每家执行接口不同，本阶段只做额度真实化。

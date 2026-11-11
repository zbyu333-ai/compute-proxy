# AI 算力指挥中心 — 代理层

小代理，拿着密钥调各家接口，吐一个 JSON 给静态 Space 读。
Space 里只放 `DATA_URL`，不放任何密钥。

## 文件

- `server.py` — 服务本身，只用 Python 标准库，不用 pip 装任何东西
- `manual.json` — 手工值：Colab / Cloady / Sakupa / Simular 四格，以及 OpenRouter、Kaggle 在没配密钥时的回落值
- `Dockerfile` — 放容器里跑用的镜像

## 本地跑

```bash
cd proxy
OPENROUTER_API_KEY=sk-or-... KAGGLE_API_TOKEN=... python3 server.py
# 打开 http://localhost:8080/api/resources.json 看一眼
# 健康检查：http://localhost:8080/health
```

不配密钥也能跑：OpenRouter、Kaggle 两格显示 `manual.json` 里的待接线值，
配了密钥后自动变实时值（5 分钟缓存一次）。

## 密钥从哪来

- **OpenRouter**：登录 openrouter.ai → Keys → 新建一个 key（`sk-or-...` 开头）
- **Kaggle**：Settings → API Tokens → Generate New Token，得到以 `KGAT` 开头的 MCP 专用 token（不是 kaggle.json 里的 Legacy API Key，那个会被 MCP 拒绝为 Unauthenticated）。`get_accelerator_quota` 调用时 arguments 必须是 `{"request": {}}`，返回里配额是 Go 时长字符串（如 `total_time_allowed: "108000s"` = 30 小时），已做解析。2026-11-11 用真实 KGAT token 本地实测通过：GPU 30 小时、TPU 20 小时、已用 0。

密钥只放进部署环境的变量里，不写进 Space、不写进聊天记录、不提交到任何公开位置。

## 放容器里跑（默认方案：Cloady 容器）

```bash
docker build -t compute-proxy .
docker run -d --name compute-proxy -p 8080:8080 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e KAGGLE_API_TOKEN=... \
  compute-proxy
```

前提：Cloady 免费档 11 月 16 日前的自查结果。
如果 16 号后免费档没了，代理不能再放 Cloady 免费容器上，另选落点。

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
- 「指派任务」真下发是第二阶段：每家执行接口不同，本阶段只做额度真实化。

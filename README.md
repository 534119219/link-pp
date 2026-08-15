# PayPal 0 元提链

使用指定代理出口和可独立选择的账单国家生成 0 元 Checkout，并提取 PayPal redirect 与
PayPal Billing Agreement 链接。项目只负责提链，不注册 PayPal 用户、不处理短信
验证码、不执行 PayPal 授权回调，也不等待付款结果。

## 流程

```text
巴西代理池
  -> 校验代理出口为 BR，并校验 ChatGPT AT
  -> 首次 Checkout 请求直接携带 plus-1-month-free
  -> 使用 DE/EUR 创建 oaics_ Checkout
  -> 提交德国账单 taxes
  -> 严格核验应付金额为 0
  -> payment_method_types 包含 paypal 时走标准 PayPal
  -> Stripe Elements Session + ConfirmationToken
  -> ChatGPT confirm(selected_payment_method_type=paypal)
  -> Stripe SetupIntent/PaymentIntent confirm
  -> 解析 paypal.com/agreements/approve?ba_token=BA-...
  -> 返回链接并停止
```

默认执行上述 `oaics_` 流程。OAICS 始终在创建 Checkout 时携带
`plus-1-month-free`，不受 Stripe 优惠策略影响。

勾选“Stripe 链提炼”后改为严格的 `cs_live_` Hosted Checkout 流程。
Stripe 模式可选择 Python 或 Go 引擎，默认为 Python；Go 只处理
`cs_live_`/`cs_test_` Stripe Session，OAICS 继续由 Python 处理。两种模式和
两个引擎都不会自动互相回退。

Stripe 优惠策略：

- `upfront`（前置优惠）：创建 Checkout 时直接携带优惠；若 Stripe `init`
  仍不是 0 元，当前 Checkout 失败，不自动转后置 update。
- `post_update`（后置 update 优惠，默认）：先创建无优惠 Checkout，通过
  Stripe `init`/Elements 确认开放 PayPal，再调用
  `/backend-api/payments/checkout/update` 施加优惠并重新 `init` 至 0 元。
- `mixed`（混合优惠）：Checkout 第 1/3/5... 轮使用前置，第 2/4/6...
  轮使用后置 update。

Python 和 Go 的 Stripe 共同主链为：

```text
init -> Elements/PayPal 检测 -> 可选 update + 重新 init -> 严格 0 元校验
     -> tax region -> ChatGPT billing snapshot -> 创建 PayPal pm_*
     -> 单次 compact confirm -> 可选 manual approve -> 轮询 redirect -> PayPal BA
```

`approve` 后只轮询原 submission，不再发起第二次 confirm。Stripe 返回
`generic_decline` 等 PayPal setup 终态时会立即归类为拒绝，不会误报为
“未返回 redirect”。

只有 Checkout 实际返回 `cpmt_` 自定义支付方式时才使用
`custom_payment_method/start` 兼容分支；空的 `custom_payment_methods` 不代表未开放
PayPal，是否开放以 `payment_method_types` 为准。

创建 Checkout 与首次提链复用同一 HTTP 会话。代理预检失败不会消耗
Checkout 次数。当 `approve blocked`、PayPal setup decline、OAICS confirm 持续
blocked、明确未返回 PayPal 方法或 Checkout 失效时，当前订单会立即停止并
创建新单；其他短暂异常会在同一 Checkout 内更换代理重试。

预检会先在同一 HTTP 会话预热 ChatGPT 页面/Cookie，再请求 `/backend-api/me`。
Cloudflare Challenge、代理出口查询失败和连接中断不在同一代理上重试：预检请求
默认最多等待 5 秒，失败后直接切换下一出口，且不消耗 Checkout 次数。预检并发默认
20 路，可通过 `HANDOFF_PREFLIGHT_TIMEOUT` 和 `HANDOFF_PREFLIGHT_CONCURRENCY`
调整。相关状态码、`cf-ray`/`cf-mitigated` 等诊断字段只写入后端脱敏日志。

## 输入

- 单个 AT 或批量 AT
- 代理出口国家与单代理池
- 独立账单国家（默认巴西出口使用德国 DE/EUR，也可手动选择巴西 BR/USD 等国家）
- OAICS 或 Stripe Hosted 提链模式
- Stripe Hosted 的 Python/Go 执行引擎
- Stripe Hosted 的前置、后置 update 或混合优惠策略
- Checkout 次数与每轮提链次数
- 批量并发数

支持 `socks5://`、`socks5h://`、`http://`、`https://`，也支持
`host:port:user:password` 裸格式。SOCKS5 输入会统一使用代理端 DNS（`socks5h`），
避免本地 DNS 模式访问 ChatGPT 超时。

Python 主链使用 Firefox 147 TLS/HTTP 指纹，并保持 UA、请求头和 Sentinel 上下文一致。

## 输出

- PayPal BA approve URL
- OAICS provider redirect URL
- ChatGPT Checkout URL
- BA token 与 Checkout session
- 批量 CSV

## API

```text
GET  /api/meta
POST /api/jobs
GET  /api/jobs/<job_id>
POST /api/jobs/<job_id>/cancel
GET  /api/jobs/<job_id>/events
GET  /api/jobs/<job_id>/events.json

POST /api/batches
GET  /api/batches
GET  /api/batches/<batch_id>
POST /api/batches/<batch_id>/cancel
POST /api/batches/<batch_id>/retry
GET  /api/batches/<batch_id>/results.csv
```

创建任务时，`country` 表示代理出口国家，`billing_country` 表示账单国家。例如
`{"country":"BR","billing_country":"DE"}` 使用巴西出口和德国账单；将
`billing_country` 改为 `BR` 即使用巴西出口和巴西账单。未传 `billing_country`
时继续使用原有自动映射。

Stripe 任务另外传入 `stripe_checkout=true`，`stripe_engine` 支持 `python` 或
`go`，`stripe_promo_strategy` 支持 `upfront`、`post_update` 或 `mixed`。例如：

```json
{
  "country": "BR",
  "billing_country": "DE",
  "stripe_checkout": true,
  "stripe_engine": "go",
  "stripe_promo_strategy": "mixed"
}
```

批次看板使用 `compact=1` 获取轻量任务字段，并通过 `after_revision=<revision>`
跳过未变化的完整响应。默认并发为 8，并发参数不设应用层上限；实际同时
执行数受批次任务数、服务器资源和可选的 `JOB_WORKERS` 部署配置影响。未配置
`JOB_WORKERS` 时，线程池容量与单批最大任务数一致，默认为 200。

## 本地运行

需要 Python 3.12+、Node.js 22.19+ 和 Go 1.26+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm ci
go build -o bin/stripe-worker ./cmd/stripe-worker
export STRIPE_GO_WORKER="$PWD/bin/stripe-worker"
python run.py
```

地址：`http://127.0.0.1:5572`

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

前台仅显示关键里程碑和警告。每个任务的完整脱敏日志保存在 Docker 持久卷的
`/data/diagnostics/<job_id>.jsonl`，容器重建后仍保留。结构化协议记录覆盖
`checkout_create`、`checkout_state`、`checkout_taxes`、`checkout_promo_update`、
`stripe_elements_session`、`stripe_confirmation_token`、`checkout_confirm`、
`stripe_intent_confirm` 和兼容分支的
`custom_payment_start`，PayPal 跳转另记录 `paypal_redirect`。记录包含 HTTP 状态、脱敏
请求/响应及上游 trace headers；不会记录 AT、Cookie、Sentinel、代理凭证或完整账单
个人信息。

## 验证

```bash
pytest -q
go test ./...
go vet ./...
python3 -m compileall -q handoff run.py wsgi.py
node --check handoff/static/app.js
```

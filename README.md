# PayPal 0 元提链

使用巴西代理出口和德国账单生成 0 元 OAICS Checkout，并提取 OAICS PayPal redirect 与
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

只有 Checkout 实际返回 `cpmt_` 自定义支付方式时才使用
`custom_payment_method/start` 兼容分支；空的 `custom_payment_methods` 不代表未开放
PayPal，是否开放以 `payment_method_types` 为准。

整条链路不调用 `/backend-api/payments/checkout/update`，并复用创建 Checkout 的
HTTP 会话。代理预检失败不会消耗 Checkout 次数。当 `approve blocked`、
OAICS confirm 持续 blocked、未返回 PayPal 方法或 Checkout 失效时，当前订单会
立即停止并创建新单；其他短暂异常会在同一 Checkout 内更换代理重试。

## 输入

- 单个 AT 或批量 AT
- 代理出口国家与单代理池（巴西出口自动使用德国 DE/EUR 账单）
- Checkout 次数与每轮提链次数
- 批量并发数

支持 `socks5://`、`socks5h://`、`http://`、`https://`，也支持
`host:port:user:password` 裸格式。

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

## 本地运行

需要 Python 3.12+ 和 Node.js 22.19+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm ci
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
`checkout_create`、`checkout_state`、`checkout_taxes`、`stripe_elements_session`、
`stripe_confirmation_token`、`checkout_confirm`、`stripe_intent_confirm` 和兼容分支的
`custom_payment_start`，PayPal 跳转另记录 `paypal_redirect`。记录包含 HTTP 状态、脱敏
请求/响应及上游 trace headers；不会记录 AT、Cookie、Sentinel、代理凭证或完整账单
个人信息。

## 验证

```bash
pytest -q
python3 -m compileall -q handoff run.py wsgi.py
node --check handoff/static/app.js
```

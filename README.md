# 双国家 PayPal 提链

使用两个独立国家出口生成 0 元 Checkout，并提取 Stripe PayPal redirect 与
PayPal Billing Agreement 链接。项目只负责提链，不注册 PayPal 用户、不处理短信
验证码、不执行 PayPal 授权回调，也不等待付款结果。

## 流程

```text
主链路国家代理
  -> 校验出口国家与 ChatGPT AT
  -> 创建原价 Checkout
优惠国家代理
  -> update 同一 Checkout
主链路国家代理
  -> 核验金额为 0
  -> 生成 PayPal redirect
  -> 解析 paypal.com/agreements/approve?ba_token=BA-...
  -> 返回链接并停止
```

首次提链复用创建 Checkout 的 HTTP 会话。代理预检失败不会消耗 Checkout 次数；
`approve blocked` 会先轮询 Stripe 的最终状态，只有当前 PayPal payment method 返回
`generic_decline`、轮询超时或 Checkout 失效时，才会放弃当前 Checkout 并创建新单。

## 输入

- 单个 AT 或批量 AT
- Checkout 主链路国家与代理池
- 优惠 update 国家与代理池
- Checkout 次数与每轮提链次数
- 批量并发数

支持 `socks5://`、`socks5h://`、`http://`、`https://`，也支持
`host:port:user:password` 裸格式。

## 输出

- PayPal BA approve URL
- Stripe redirect URL
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

需要 Python 3.12+ 和 Node.js 20+。

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

前台仅显示关键里程碑和警告。每个任务的完整脱敏日志与 Stripe confirm 诊断保存在
Docker 持久卷的 `/data/diagnostics/<job_id>.jsonl`，容器重建后仍保留。

## 验证

```bash
pytest -q
python3 -m compileall -q handoff run.py wsgi.py
node --check handoff/static/app.js
```

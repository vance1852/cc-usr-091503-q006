# 婴儿洗护事件记录服务

面向育婴师移动端的洗护事件记录服务（Flask + SQLite）。在每次洗护**开始前**核对
婴儿身份（腕带扫码）、家长授权版本、用品批次、护理人员资质和特殊注意事项；
洗护过程中按实际顺序保存开始、暂停/恢复、用品更换、观察与结束事件，并为主管提供
一次洗护的全链路追溯。

系统只做**记录与留痕，不做医学判断**：现场观察原样保存，出现不适迹象时强制终止、
启动复核并保存家长通知回执。

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # 默认 127.0.0.1:5000，自动建表
.venv/bin/python -m pytest -q     # 测试
```

## 业务规则

开始前核对（任一不通过均返回 4xx，**不得开始**）：

1. **身份一致**：腕带扫码必须与婴儿档案一致（`identity_mismatch` 拒绝）。
2. **家长授权**：必须存在系统内**显式有效**的最新授权版本；纸面服务单的默认勾选、
   家长口头顾虑均不构成授权（`authorization_missing` 拒绝）。
3. **用品批次**：用品存在且未被中心停用；停用批次在开始时和换品时都会被拦截
   （`product_deactivated`）。
4. **人员资质**：在册、未停用、资质未过期。
5. **特殊注意事项**：存在注意事项时必须现场确认知悉（`note_acknowledged`）。

授权管理：

- 家长可在**操作前**缩小授权范围（只能是原子集），缩小产生新版本并保留版本链；
  洗护进行中不允许变更授权。
- 家长可随时撤回授权；撤回后进行中的会话拒绝一切普通事件，必须立即终止并复核。

过程控制：

- 事件状态机：`in_progress ⇄ paused → ended | aborted`，事件带服务端严格递增 `seq`。
- 出现不适迹象（observation 携带 `discomfort: true`）时立即自动终止、生成复核单
  并向家长发送带回执的通知；观察内容（体征、描述）原样保存。
- 用品扫码令牌（`scan_token`）全局唯一：重复扫码不会二次消耗，也不会生成两次换品
  事件；网络重传由幂等键拦截。
- 事后更正只追加（corrections），必须注明原事件、字段、原值、新值与原因，
  原始事件永不修改。

## 幂等设计（网络重传安全）

| 场景 | 幂等凭证 | 重传行为 |
|---|---|---|
| 开始洗护 | 请求级 `idempotency_key` | 返回原会话，`created=false`，不重复消耗用品 |
| 追加事件 | 事件级 `client_event_id` | 返回原事件，状态机不重复迁移 |
| 用品扫码 | 全局唯一 `scan_token` | 同会话返回原换品记录；跨会话报 `scan_token_consumed` |

## 接口

主数据：`POST /api/babies`、`POST /api/staff`、`POST /api/products`、
`POST /api/products/<id>/deactivate`、`POST /api/babies/<id>/notes`

授权：

- `POST /api/authorizations` — 建立授权（`scope` 省略表示全部允许）
- `POST /api/babies/<id>/authorization/narrow` — 操作前缩小范围（新版本）
- `POST /api/authorizations/<id>/withdraw` — 撤回授权

洗护：

- `POST /api/sessions` — 开始（开始前核对 + 首批扫码消耗，幂等）
- `POST /api/sessions/<id>/events` — 追加事件
  （`pause` / `resume` / `product_change` / `observation` / `end`）
- `POST /api/sessions/<id>/abort` — 立即终止并启动复核
- `GET  /api/sessions/<id>` — 主管全链路追溯
- `POST /api/sessions/<id>/corrections` — 事后更正
- `POST /api/notifications/<id>/confirm` — 家长确认收到
- `POST /api/reviews/<id>/resolve` — 主管完成复核

### 典型流程

```bash
# 建档 + 授权
curl -s localhost:5000/api/babies -H 'Content-Type: application/json' \
  -d '{"name":"小宝","wristband_code":"WB-001"}'
curl -s localhost:5000/api/authorizations -H 'Content-Type: application/json' \
  -d '{"baby_id":1,"granted_by":"家长"}'

# 开始（带幂等键与首批扫码；断网重传同一请求体安全）
curl -s localhost:5000/api/sessions -H 'Content-Type: application/json' -d '{
  "idempotency_key":"wash-20260919-001","baby_id":1,"wristband_code":"WB-001",
  "staff_id":1,"note_acknowledged":true,
  "scans":[{"product_id":1,"scan_token":"scan-A"}]}'

# 中途换品
curl -s localhost:5000/api/sessions/1/events -H 'Content-Type: application/json' -d '{
  "type":"product_change","client_event_id":"evt-2",
  "payload":{"product_id":2,"scan_token":"scan-B","reason":"余量不足"}}'

# 主管追溯：授权链/步骤顺序/用品批次来源/复核/通知回执/更正
curl -s localhost:5000/api/sessions/1
```

## 数据模型（schema.sql）

`babies` · `staff` · `products`(批次/停用) · `special_notes` ·
`authorizations`(版本链 active/superseded/withdrawn) · `sessions`(授权快照+幂等键) ·
`events`(严格 seq) · `product_uses`(scan_token 唯一) · `reviews` ·
`notifications`(回执) · `corrections`(只追加)

追溯接口一次性返回：会话与开始时授权快照、婴儿与人员、授权版本链、
注意事项、有序事件、用品消耗（批次/供应商/当前状态）、复核单、通知状态与回执、
更正记录。

## 测试

`tests/test_carelog.py` 覆盖：授权缺失/身份不一致/停用用品/资质过期拦截、
操作前缩小授权（禁止扩大）、**中途换品及换品重放**、**授权撤回强制终止复核**、
**开始/事件/扫码三级网络重传幂等**、不适自动终止与通知、事后更正只追加、
全链路追溯，共 18 个用例。

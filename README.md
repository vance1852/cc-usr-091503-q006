# 婴儿洗护事件记录服务

婴儿首次洗护前的身份、家长授权版本、用品批次、护理人员资质与特殊注意事项核对，
并按实际顺序保存开始、暂停/恢复、用品更换、现场观察与结束事件的 Flask + SQLite 服务。

## 业务规则

- **开始前核对（缺一不可）**：婴儿扫码与第二标识一致；护理人员资质在有效期内；
  家长最新授权为 active 且覆盖计划用品；产品与批次均未被中心停用/召回、未过有效期。
  授权缺失、身份不一致、用品停用、资质失效一律拒绝开始。
- **授权按版本留痕**：纸面单勾选"默认用品"与家长口头疑虑都可记录；家长只能在操作前
  **缩小**授权范围（生成新版本，旧版本永久保留），不能扩大；撤回授权后不得开始，
  洗护进行中撤回则**立即终止并进入主管复核**、通知家长。
- **不适处置**：现场观察到不适迹象时立即中止、进入复核并通知家长；系统**不做任何
  医学判断**（事件中 `medical_judgment` 恒为 null），只如实保存观察与通知回执
  （含通道故障时的失败回执）。
- **事件只追加**：`start / pause / resume / product_change / observation / abort /
  notification / end / correction`，会话内严格递增序号。事后更正只能追加 correction
  事件，注明原记录 id、字段、旧值/新值与原因，原记录不可修改。
- **幂等**：开始与各事件靠 `client_event_id` 去重；用品消耗靠
  `(session, lot, client_request_id)` 唯一约束；同一会话重复扫同一批次不重复消耗、
  不生成第二次操作；网络重传原样返回首次结果（`replayed: true`）。
- **主管追溯**：一次洗护可查授权版本、身份核对、执行步骤顺序、用品批次与供应商、
  异常处置和家长通知状态。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m flask --app washcare.api:create_app run   # 首次启动自动建库+演示数据
# 或指定数据库：
WASHCARE_DB=/var/lib/washcare/app.db .venv/bin/python -m flask \
  --app 'washcare.api:create_app' run --host 0.0.0.0
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/precheck` | 操作前核对（不落数据），返回问题清单与注意事项 |
| GET  | `/api/babies/<code>/consents` | 授权版本链 |
| POST | `/api/consents/narrow` | 开始前缩小授权（生成新版本） |
| POST | `/api/consents/withdraw` | 撤回授权（进行中则自动中止+复核+通知） |
| POST | `/api/sessions` | 开始洗护（幂等键 `client_event_id`） |
| POST | `/api/sessions/<code>/events` | 追加 pause/resume/product_change/observation/end/abort |
| POST | `/api/sessions/<code>/corrections` | 事后更正（只追加，须注明原因与原记录） |
| GET  | `/api/sessions/<code>/trace` | 主管完整追溯 |
| POST | `/api/admin/products/<code>/deactivate` | 中心停用产品 |
| POST | `/api/admin/lots/<code>/deactivate` | 中心停用/召回批次 |

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

测试重放三类关键场景：

1. **中途换品**：沐浴露 → 暂停 → 润肤乳，顺序、批次来源、`from_lot` 链完整；
   换品请求网络重传只消耗一次；重复扫码不生成第二次操作。
2. **授权撤回**：开始前撤回不得开始；进行中撤回立即终止、进入复核并留下通知回执。
3. **网络重传**：开始/暂停/观察中止的重复 POST 均命中幂等键，不产生重复会话、
   重复消耗或重复通知（含通知通道故障时失败回执仍落库）。

## 代码结构

- `washcare/db.py` — SQLite schema、连接、演示数据（含纸面默认单+家长口头疑虑场景）
- `washcare/service.py` — 核对规则与事件状态机（幂等、版本化授权、中止复核、更正）
- `washcare/notifications.py` — 通知发送器与回执（含模拟故障的 FailingNotificationSender）
- `washcare/api.py` — Flask 移动端接口
- `tests/` — pytest 重放测试

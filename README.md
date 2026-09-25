# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 配对链（跨医院成组交换）

协调员一次提交 2-4 组器官与患者。建链前先逐组校验血型、器官、医院与有效期（且确认截止时间不得晚于任何器官有效期），全部对得上才建立待确认链，任一组不合格则整链不建、不锁任何器官。建链后各组器官进入 `chain_pending`，接收医院在约定确认时间前逐组确认；任一组拒绝或超时，该环节留在链上（器官保持锁定），其余环节的未过期器官恢复可选（已过期则记为 `expired`）。全部确认后链转为 `confirmed`，器官转为 `allocated`。每次确认、退回、超时和器官恢复都写入链事件，审计员可逐条追溯。

- `POST /api/chains`：协调员建链，body 为 `{"legs": [{"donor_id", "candidate_id"}, ...], "confirm_deadline": "ISO 时间"}`；校验失败返回 409 及 `details.issues` 分组明细。
- `POST /api/chains/{id}/legs/{seq}/confirm`、`reject`：接收医院确认或退回（`reason` 必填）。
- `GET /api/chains/{id}`：链路顺序、各方状态与截止时间；医院仅可见与本机构相关的链，其他组患者姓名脱敏。
- `GET /api/chains/{id}/audit`：审计员查看链事件（含每次退回）。

代码按职责分开维护：配对判断在 `chain_matching.py`（纯函数），链记录在 `chain_store.py`（chains / chain_legs / chain_events 三表），协调台编排与页面在 `app.py` 和 `static/index.html`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。

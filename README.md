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

### 跨医院成组交换配对链

协调员一次提交 2-4 组「器官 → 患者」。配对判断（`chain_matching.py`）逐组校验血型兼容、器官一致、跨医院（来源与接收医院不同）、器官在确认截止前有效，并要求全部医院转出/接收一一对应成环；任何一组不通过都**不会建链**，错误按组返回。全部通过后建立待确认链（`chain_store.py`，独立于单笔 allocation 记录），器官进入 `locked` 防止等待期间被单笔分配拿走。

- `POST /api/chains`（coordinator）：请求体 `{"groups":[{"donor_id","candidate_id"}...], "deadline", "note"}`，校验全过才建链。
- `POST /api/chains/{id}/legs/{position}`（对应接收医院）：在截止时间前逐组确认。
- `POST /api/chains/{id}/legs/{position}/reject`（对应接收医院）：任一组拒绝即断链；超时由系统在访问链接口时惰性结算。
- 断链时**该环节留存**（环节标记 `rejected`/`timed_out`，器官置 `held`），其余未过期器官恢复 `available`（已过期的置 `expired`）。
- `POST /api/donors/{id}/release-held`（coordinator）：处置留存器官，重新放回可选池，全程审计。
- 全部环节在截止前确认后，整链一次性落地为各自的 `accepted` allocation，可继续走转运/交接/植入流程。
- `GET /api/chains`、`GET /api/chains/{id}`、`GET /api/chains/{id}/audit`：链路顺序、各方状态、截止时间与每次确认/退回的审计追溯（医院只能看本院参与的链，且患者姓名按既有规则掩码）。
- 首页 `/` 协调台可直接建链、逐组确认/拒绝、释放留存器官并查看链审计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。

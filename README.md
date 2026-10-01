# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。
- `POST /api/sources/{id}/updates`：工作人员选定来源、填写原因，发起撤回（`withdrawal`）或更正（`correction`），响应先列出引用它的藏品、流转事件和权利主张。
- `POST /api/source-updates/{id}/confirm`：确认生效。确认前出现新增引用时，请求停在 `pending_retry` 并返回新对象；`POST .../refresh` 重新核对后可再次确认。
- `POST /api/source-updates/{id}/retry`：批量写入中断后续做，只处理未完成对象，已完成对象不重写、不重复记审计。
- `GET /api/source-updates/{id}`、`GET /api/sources/{id}/updates`、`GET /api/sources/{id}/impact`：查看影响范围与处理进度。

公众看不到持有人和内部事件；主张人只能查看自己的主张，对来源更新的影响也只能看到与自己主张有关的部分；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

来源更新生效后，依赖该来源的未完成主张置为 `invalidated` 并可由审查员退回 `submitted` 重算；已完成返还（`resolved_return`/`rejected`）不受影响，历次快照仍可查询。同一来源的并发撤回/更正先到者生效，后到者收到 409 及最新影响范围；重复提交已生效的请求不会写第二遍审计。

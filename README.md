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
- `POST /api/sources/{id}/retractions`：发起来源撤回，先列出引用它的藏品、流转事件和权利主张。
- `GET /api/retractions`、`GET /api/retractions/{id}`：撤回请求与影响范围（主张人只看到与自己主张有关的影响）。
- `POST /api/retractions/{id}/confirm`：确认撤回；确认前若有新引用会停在“待重试”并纳入新对象；批量写入失败后保留进度，重试只续做未完成对象，重复提交不写第二遍审计（可传 `fail_after` 模拟批量失败）。
- `POST /api/sources/{id}/update`：来源更正，依赖它的未完成主张失效并重算，已完成返还和历次快照仍可查。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张和与自己主张有关的撤回影响；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。两人同时提交同一来源的撤回时先到者生效，后到者拿到最新影响范围。

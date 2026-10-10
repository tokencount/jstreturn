# jstreturn
次品维修配件管理系统 — jstreturn

## Daily inventory automation

The workspace-owned entrypoint is `scripts/daily-inventory-refresh-runner.mjs`.
It implements the 66/88/99 login/export workflow, validates all three Excel
files, merges duplicate SKUs with source provenance, and only then calls the
transactional `/api/inventory/upload` endpoint. It provides atomic locking with
TTL/heartbeat recovery, scheduled-window idempotency, timeout, checkpoints,
step logs, and per-run `result.json`.

Safe checks never export or upload:

```sh
node scripts/daily-inventory-refresh-runner.mjs --healthcheck --no-notify
node scripts/daily-inventory-refresh-runner.mjs --dry-run --no-notify
node scripts/daily-inventory-refresh-runner.mjs --login-check --no-notify
```

Production upload requires `JSTRETURN_BASE_URL` and protected Keychain service
`jstreturn-admin-login` containing JSON keys `name` and `token`. JST account
credentials are read from the existing protected per-account Keychain services
and are never written to results or logs.

# 员工账号

- Admin 在「账号」页创建员工时填写 DingTalk `userid`（不是姓名）；同一员工可绑定多个本系统账号。
- 新账号使用 Admin 提供的初始 Token 登录，必须立即设置个人密码。设置后初始 Token 失效；Admin 可从账号页重设密码。
- Admin 可通过 `POST /api/users/_/deactivate-employee`（JSON：`{"dingtalk_user_id":"..."}`）停用该 DingTalk 员工 ID 下的所有 jstreturn 账号，历史记录保留，现有会话即时失效。
- DingTalk 离职事件自动触发仍需配置实际事件/API 来源；不能仅凭员工姓名推断离职。

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

# 采购配件导出日期范围

- 页面：采购导出 → 开始业务日期 / 结束业务日期；均含当天。
- 日期口径：退件单 `defective_items.business_date`，马来西亚业务日期；不是 `created_at` 或导出当天。
- 两项留空：全部业务日期（包括无业务日期的历史单）。只填一项：单边不限。指定范围时，无业务日期的单不纳入。
- 开始晚于结束、日期格式无效：中文提示，HTTP 400；在查询数据库之前拒绝。
- 无采购需求：下载包含中文说明的 ZIP，页面明确提示；不生成无数据 Excel。
- API：`GET /api/exports/purchase?start_date=2026-08-01&end_date=2026-08-31`；`summary_only=true` 及 `/purchase/preview` 使用相同范围。
- 兼容原 `business_date=YYYY-MM-DD` 单日参数；不允许与非空范围参数混用。
- 注意：旧代码“不传日期”实际默认当天；此次按需求改为全部，不再由页面自动补当天。
- JSON/下载汇总头新增 `start_date`、`end_date`、`date_range`。原 `business_date` 字段/头保留兼容日期戳，范围以新增字段为准。
- 文件名带日期范围；HS/HE/HU 仓库映射、Excel 两张表、A/B/C 数据及其余模板字段保持原样。
- 计算顺序：读取全局 PENDING + READY → 运行既有预留算法 → 按业务日期筛选 PENDING 缺件 → 按 part_code 汇总。其他日期 READY 占用的库存不得因筛选被释放；不更新维修单、库存或状态。
- app-src 已有 E 仓限定分配，因此使用其 matcher 的 ELIGIBLE_INVENTORY_SQL；app 较旧 matcher 无该规则，保留该树原库存来源。没有覆盖两树其他功能差异。

## 验证

`tests/test_purchase_date_range.py` 覆盖双边/单日/单边/空日期/非法与反向/空结果/预览一致性/权限/跨日期预留。
`node tests/test_purchase_date_range_ui.cjs` 验证实际 app() JS：日期参数、默认全部、单边、反向阻止请求、后端错误、空结果下载及手机 CSS。

本任务未部署：本沙箱无 git 命令、两源码树无 .git，已指定的受限 Render 凭据路径不可见；不能凭本地代码或测试结果宣称已上线。未读取外部凭据、未更改生产业务记录或 cron/Gateway。

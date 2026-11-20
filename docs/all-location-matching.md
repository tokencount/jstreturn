# 全仓维修配件匹配（Cc msg1275，2026-11-20）

维修 READY/PENDING、stock/available/reserved/missing 与采购 preview/export 共用 `ELIGIBLE_INVENTORY_SQL`，汇总所有仓位，不限 E 开头。每 SKU 仅汇总一次 inventory_locations，不能再叠加 inventory_snapshot；同仓位多行正常求和。沿用既有正数量语义：零/负数不增加可分配库存，也不抵扣正数。只有完全没有明细行的 legacy SKU 才回退到 max(snapshot 数量, 0)，不受仓位前缀限制。库存仓位展示不变。

Pallet 优先级、整单预留、advisory transaction lock、采购逐单缺件与日期范围语义不变。正常打开 READY/PENDING 列表会经既有锁重建派生状态，不编辑原始单据、需求数量或库存。

回归：安装 requirements.txt 及测试依赖 pytest、duckdb，然后执行 `python -m pytest`。新测试在 DuckDB 执行同一生产 SQL，覆盖非E、混合、多行、零负、legacy与竞争；生产验收另外核对 PostgreSQL 实际输出。

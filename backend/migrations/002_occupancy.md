# 初始结构

SQLAlchemy启动创建以下业务表：service_periods, service_cases, business_calendars, sla_runs, delivery_schedules, deliveries、users、audits。Source专属JSON字段由对应validation严格校验；Policy为不可覆盖的版本记录；Run保存source/policy冻结快照；Period仅用于已有期间选择器，不计入核心复杂度。已有数据库升级需先备份，后续字段演进应以独立迁移执行。

# 002 占用去重分摊

新增四张表，由 `Base.metadata.create_all` 在启动时自动建立，已有库无需手工改动列：

- `occupancy_checkpoints`：每run一行，保存扫描线检查点（事件游标、营业时窗栈、跨段在途单、累计精确分摊、峰值），worker中断后续跑据此接回，已分摊事件不会二次处理。
- `occupancy_segments`：重叠段去重明细，`(run_id, ordinal)` 唯一，保证重试不重复写段；按 `(run_id, day)` 索引支持报告页按日期分页查询。
- `occupancy_apportion`：日期×小组整数分摊明细，`(run_id, day, group_name)` 唯一；仅当全局值与分摊明细通过守恒发布闸门后才写入。
- `occupancy_traces`：逐笔在途单的去重分摊/未去重交集分钟与参与重叠段数，支持按底稿键或小组检索。

口径：冻结底稿与口径快照；扫描线事件切分重叠段（O(N log N)，不逐分钟展开）；重叠段按当时各小组在途单数占比分摊；全局分钟四舍五入（半数上进）后两级最大余数法，先分业务日期再分小组，余数大者先补、同余按日期或组名升序。原SLA核算（sla_runs.result.rows/summary）口径与字段保持不变，占用结果位于 `result.occupancy`。

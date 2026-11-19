# 临汀服务时窗交付簿

数据服务请求时间→业务时钟核算→站内定时交付；含时区与跨日，非工单客服功能

## 业务场景与工作流
模拟客户：临汀研究协作组，12位业务与分析人员。每月研究明细先从`service-calendar-202611.csv`录入底稿，经口径冻结、Python核算、负责人复核后向业务阅读者交付站内CSV。原有流程依靠人工表格抄算，痛点为口径版本难追溯、重复计算易错、复核与交付脱节。

模块：业务总览、业务底稿、新建口径版本、异步核算与复核、定时交付、站内收件箱、审计。底稿可新增/修改/删除；口径仅新增版本；报告提交时冻结来源与版本并保存SHA256。定时交付引用已复核报告快照，worker到期写入数据库，业务阅读者下载。

## 算法口径
工作日/假日/午休交集分钟与SLA超时分层透视。周五到周一含假日和午休手算；负时段拒绝；边界分钟。

业务时钟 · 午休与假日显式扣除。输入：业务底稿与口径JSON；输出：summary、透视rows、method、warnings。算法实现在`backend/src/services/calculation.py`，输入校验在`validation.py`。固定seed=42，240条合成数据；未对接真实业务系统。修改源数据和新建因子/口径不会改变已提交报告。

### 服务占用去重分摊（多组在途重叠）
多组同时有待办时，逐单相加会把重叠的服务占用重复计算。本模块在原SLA之外并行产出`result.occupancy`，原SLA口径与字段完全不变：
- **冻结底稿**：与报告同一快照，运行期间改源数据不影响本次结果。
- **扫描线分段，不逐分钟展开**：在途区间与营业时窗统一转口径时区后生成边界事件，按时间排序切分重叠段，复杂度 O(N log N)，分段数与事件数同阶；十万单并发实测约2秒、数百段（见`backend/tests`）。
- **按在途单数分摊**：一个重叠段的去重分钟，按当时各小组在途单数占比分给各组（组g分得=段分钟×组g在途单数/段内总在途单数）；午休、假日、非工作日不落任何分段。
- **全局四舍五入与两级最大余数法**：全局分钟按半数上进取整后，先分业务日期、再分小组；余数大者先补，真同余按日期ISO或组名升序确定归属，整数分钟严格守恒、结果可复算。
- **中断续跑、重试不重复分摊**：扫描状态（事件游标、营业时窗栈、跨段在途单、累计精确分摊、峰值）按检查点落`occupancy_checkpoints`；worker崩溃后running任务重新入队，从游标接回跨段在途单。重叠段落`occupancy_segments`，`(run_id,ordinal)`唯一，重试幂等不重复写。
- **发布闸门**：仅当全局值与日期×小组分摊明细齐全且守恒（小组和=日期和=全局整数分钟）才写`occupancy_apportion`并置completed；失败回滚发布物、保留检查点，报告页显示`occupancy_error`且不影响原SLA结果。
- **可查可导**：前端「占用分摊报告」页（`#occupancy`）提供分摊明细、日期合计、重叠段按日期分页、逐笔追溯（底稿键/小组筛选）与两类CSV；后端端点在`/runs/{id}/occupancy/*`。核心引擎在`src/services/occupancy.py`（纯标准库），持久化编排在`src/services/occupancy_run.py`。

## 一天的验收剧本
1. 09:00 admin查看240条seed底稿与具名业务期间。
2. 09:10 analyst添加`service-calendar-202611.csv`的一条具名业务记录。
3. 09:20 admin在口径版本页登记新版本；旧版本保留。
4. 09:30 analyst命名“十一月核算”并选择期间/版本排队。
5. 09:31 Redis worker真实执行Python计算，展示汇总/透视与口径边界。
6. 09:40 admin核对数值后填写至少4字复核说明。
7. 09:45 admin建立60秒周期计划，worker到期生成站内持久化交付。
8. 09:46 viewer在站内收件箱下载交付CSV；审计页查看操作者与时间。

## 快速启动
```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```
前端 http://localhost:18252 ，后端 http://localhost:19252/docs ，数据库宿主端口17752。中文父目录可用。恰好5服务frontend/backend/worker/db/redis；均有真实healthcheck。停止并清空演示数据：`docker compose down -v --remove-orphans`。

|角色|邮箱|密码|权限|
|---|---|---|---|
|admin|admin@demo.local|Demo2026!|全部底稿、口径版本、核算、复核、调度|
|analyst|analyst@demo.local|Demo2026!|底稿增改删、核算、导出；无复核/调度/口径写权限|
|viewer|viewer@demo.local|Demo2026!|只读；仅可下载已复核报告与站内交付|

## 工程结构与开发
Python3.11/FastAPI/SQLAlchemy/Redis，PostgreSQL16；前端原生HTML/CSS/JS ES modules，Nginx代理`/api/`剥前缀至后端。`frontend/src`按api/stores/pages/components/hooks/utils/router/constants/types拆分；`backend/src`按models/schemas/services/controllers/routes/middlewares/config/utils/constants拆分；`backend/scripts/seed_demo.py`自然键幂等且不覆盖用户修改。SQLAlchemy启动创建初始结构，迁移说明在backend/migrations。

后端开发：在backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET，`uvicorn src.main:app --reload`；前端可用Nginx或带同等代理的静态开发服务器。前后端全部业务计算在Python。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、JWT_SECRET（部署替换）、DATABASE_URL、REDIS_URL、SEED_DEMO。`.env`不入Git。数据库/Redis/共享产物为命名卷。共享角色枚举admin/analyst/viewer与状态queued/running/completed/failed分别定义于backend/src/constants/enums.py和frontend/src/constants/enums.js。两个中间件：统一可恢复校验错误、每IP每分钟360请求限流。

## 已知边界
定时分发为站内收件箱；一名worker轮询执行，无外部邮件。调度发送已复核固定报告快照，不自动重算新数据。时间显示Asia/Shanghai。报告导出适合小型数据；底稿全量加载240行演示数据。Google Fonts不可用时回退serif。后端API以专属字段对象收发，页面通过业务输入控件编辑，不要求用户写JSON。新增来源期间自动可选；专属字段和算法严格校验。JWT演示有效期2小时。

MIT License。

## 实际实体映射
来源记录（Source，专属业务字段校验）、口径版本（Policy）、核算报告（Run）、交付计划（Schedule）、交付（Delivery）均经数据库→模型→请求schema→service→route/controller→frontend API→store→page。Period为已有来源期间字典，在任务期间选择器使用；不单独作为复杂度计数。底稿JSON为小型多字段单记录存储，不虚构未落库的子实体。

实际表：期间字典 service_periods；专属来源 service_cases；冻结口径 business_calendars；异步报告 sla_runs。

占用去重分摊配套表（002迁移，启动自动建表）：`occupancy_checkpoints`（每run一行的扫描检查点，接回跨段在途单）、`occupancy_segments`（重叠段去重明细，run+ordinal唯一）、`occupancy_apportion`（日期×小组整数分摊，过发布闸门才写入）、`occupancy_traces`（逐笔在途单去重/未去重分钟与参与段数）。

## 离线测试
无外部依赖，标准库即可运行（无需数据库/Redis）：
```sh
cd backend
python3 -m tests.test_occupancy        # 引擎：去重、分摊、取整、续跑、十万单性能、发布闸门（59项）
python3 -m tests.test_occupancy_persist # 编排：检查点、幂等写段、崩溃续跑、分页筛选（40项，内存假库）
```

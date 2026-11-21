# 数据质量监控与元数据治理平台

面向数据质量标准定义、自动巡检、异常告警、元数据采集与数据目录构建的数据质量治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 质量规则定义 | `dq_rule_def` | 质量规则 | 规则编号、规则名称、目标表名 |
| 巡检任务管理 | `dq_check_run` | 巡检任务 | 任务编号、关联规则集、数据范围 |
| 质量事件管理 | `dq_incident` | 质量事件 | 事件编号、关联规则、触发时间 |
| 元数据采集器 | `metadata_crawler` | 采集器配置 | 采集器编号、数据源类型、连接地址 |
| 数据表目录 | `table_catalog` | 表目录 | 表编号、表名称、所属库名 |
| 字段画像分析 | `column_profile` | 字段画像 | 画像编号、关联表名、字段名称 |
| 血缘图谱 | `lineage_graph` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 指标波动监控 | `metric_monitor` | 监控指标 | 指标编号、目标表名、监控字段 |
| 质量评分卡 | `dq_scorecard` | 评分记录 | 记录编号、目标表名、完整性得分 |
| 数据分类标签 | `tag_classify` | 分类标签 | 标签编号、标签名称、分类维度 |
| 规则模板库 | `dq_template` | 规则模板 | 模板编号、模板名称、规则类型 |
| 异常模式识别 | `anomaly_pattern` | 异常模式 | 模式编号、模式名称、检测方法 |
| 质量报告生成 | `dq_report` | 质量报告 | 报告编号、报告周期、覆盖表数 |
| 元数据变更对比 | `metadata_diff` | 变更记录 | 变更编号、变更对象、变更类型 |
| 告警通知通道 | `dq_alert_channel` | 告警配置 | 配置编号、关联规则集、通知方式 |
| 质量基线管理 | `dq_baseline` | 基线定义 | 基线编号、目标表名、基线指标 |
| 主数据实体注册 | `mdm_entity` | 主数据实体 | 实体编号、实体名称、业务域 |
| 质量趋势看板 | `dq_trend` | 趋势记录 | 记录编号、目标表名、日期 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 质量报告 v1 → v2 兼容

v1 日报只有汇总列（覆盖表数/通过规则数/失败规则数/趋势分析），v2 把它们
迁成**带来源与结构版本的报告快照**，报告编号保持不变，两种结构可以同时按原编号读取。

- 领域逻辑：`backend/app/services/report_snapshot.py`
- 路由：`backend/app/routers/report_compat.py`（固定路径排在 `/api/{module}` 之前）
- 前端核对页：`/report-compat`（`frontend/src/views/ReportCompat.vue`）

关键行为：

| 场景 | 行为 |
| --- | --- |
| 存量迁移 | 按“日报日期”回到 `dq_scorecard` / `dq_check_run` / `dq_trend` 找来源；找不到的指标保留原值并标 `存量兼容缺项` |
| 同一编号多版本 | `GET /api/report/numbers/{报告编号}` 同时返回 v1 汇总行和 v2 快照；`.../effective` 返回实际取得的报告 |
| 时点隔离 | 快照固定创建时的 `store.journal_seq()`，事件证据用 `store.rows_as_of` 重建，不读取实时状态 |
| 部署中断 | 逐份提交；`POST /api/report/migrations/{id}/resume` 沿用原数据时点补齐，`/rollback` 作废本次快照并回退 v1 |
| 签收 | `POST /api/report/snapshots/{id}/sign`，确认结果同步到 `GET /api/report/incident-review` |
| 逐项重做 | `POST /api/report/snapshots/{id}/recompute` 生成新修订快照，已签快照不可变 |
| 事件侧回查 | `GET /api/report/incidents/{id}/evidence`、`GET /api/report/daily/{日期}`（含缺来源旧日报） |
| 跨版本核对样例 | `GET /api/report/fixtures/v1-export` 取一份已导出的 v1 CSV，`POST /api/report/import/v1` 导入并返回按原编号的实际取得报告 |

测试：`backend/tests/test_report_snapshot.py`（`cd backend && .venv/bin/python -m pytest -q`）。

领域：数据工程
子方向：数仓与数据湖

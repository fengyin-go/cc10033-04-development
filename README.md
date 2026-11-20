# 维度建模开发中心

面向数据仓库建设场景，提供维度建模、事实表设计、缓慢变化维和汇总层构建的建模平台

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
| 维度定义 | `dim_definition` | 维度表 | 维度编码、维度名称、主键定义 |
| 维度属性 | `dim_attr` | 属性字段 | 属性名称、所属维度、数据类型 |
| 事实设计 | `fact_design` | 事实表 | 事实名称、业务过程、粒度声明 |
| 度量配置 | `measure_config` | 度量字段 | 度量名称、所属事实、度量类型 |
| SCD处理 | `scd_handler` | SCD策略 | 策略名称、目标维度、SCD类型 |
| 代理键 | `surrogate_key` | 键值定义 | 键值名称、目标维度、生成策略 |
| 层级构建 | `hierarchy_build` | 层级关系 | 层级名称、所属维度、父子字段 |
| 星型查询 | `star_query` | 查询定义 | 查询名称、事实表、关联维度 |
| 汇总构建 | `agg_summary` | 汇总表 | 汇总名称、源事实表、汇总维度 |
| 桥接关联 | `dim_bridge` | 桥接表 | 桥接名称、多对多维度、关联事实 |
| ETL映射 | `etl_mapping` | 映射规则 | 规则名称、源表、目标表 |
| 快照计划 | `snapshot_plan` | 快照策略 | 策略名称、目标表、快照周期 |
| 模型血缘 | `model_lineage` | 依赖关系 | 关系ID、上游模型、下游模型 |
| 一致性维度 | `conform_dim` | 标准维度 | 标准名称、维度主题、标准键值 |
| DataVault | `data_vault` | Vault模型 | 模型名称、Hub实体、Link关系 |
| 多维立方 | `cube_builder` | Cube定义 | Cube名称、源事实表、维度组合 |
| 变更日志 | `slow_change_log` | 维度变更 | 变更ID、目标维度、变更字段 |
| 日期维度 | `date_dim_gen` | 日期记录 | 日期键、完整日期、年份 |
| 模型校验 | `model_validate` | 校验规则 | 规则名称、目标模型、校验类型 |
| 部署流水线 | `deploy_pipeline` | 部署任务 | 任务名称、目标模型、部署环境 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## SCD 变更履历与生效区间

`scd_history` 是只追加的履历台账，`scd_effective_interval` 是按 SCD 策略和履历
推导出的生效区间投影，`scd_history_meta` 记录每个维度的履历版本（用于并发校验）。
三者都是内部表，不出现在业务模块列表里。

| 接口 | 说明 |
| --- | --- |
| `GET /api/scd-history/dimensions` | 各维度履历 / 区间概况 |
| `GET /api/scd-history/roles` | 维护人角色与更正权限 |
| `GET /api/scd-history/{dim}/history` | 只读履历（返回 `revision`） |
| `POST /api/scd-history/{dim}/history` | 登记 / 补录履历，原子重算区间 |
| `GET /api/scd-history/{dim}/intervals` | 生效区间投影 |
| `GET /api/scd-history/{dim}/as-of?date=` | 追溯某天：取值、来源履历、关联更正 |
| `GET /api/scd-history/{dim}/integrity` | 区间重叠、投影漂移、只读原值校验 |
| `POST /api/scd-history/{dim}/repair` | 按履历重算并整体替换投影 |

要点：

- **只读原值**：首发发布的旧值 / 新值 / 业务日期写入后不可修改，补录和更正都是新增履历。
- **区间推导**：SCD2/3/6 按业务日期切分区间，SCD1 只保留最新值，SCD0 拒绝变更。
- **同日更正**：同一天只保留一个生效版本，后登记者胜出，原记录标记「已被同日更正」。
- **迟到补录**：补录日期早于已发布版本时，原区间结束日期被截短，来源履历不变。
- **并发更正**：`期望版本` 与当前 `revision` 不一致时返回 409，不会落半套数据；
  校验失败会同时回滚履历、镜像日志和投影。
- **权限**：`data_steward` / `dim_owner` 可发布和更正，`publisher` 只能发布，
  `viewer` 只读。

变更日志模块（`slow_change_log`）的登记会自动写入履历并生成镜像日志，
「追溯变更」动作返回当日归属、生效区间和修订来源。工作台的变更日志、SCD 处理、
维度定义、维度属性页面下方都带有履历面板。

领域：数据工程
子方向：数仓与数据湖

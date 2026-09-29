# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 资源额度（机器时与任务数）

额度按「周期 + 对象」建池，对象支持课程（`course`，键为课程/项目编码）、项目（`project`）和个人（`user`）；任务同时受课程池与个人池约束，取交集。

- **提交只提示**：`POST /api/compute/tasks` 返回 `resource_preview`（预计机器时、预计任务数与周期键），不占用任何额度。
- **领取才预留**：`POST /api/compute/tasks/claim` 在同一即时事务内原子预留机器时与任务数。额度不足的队首任务保持排队，领取会跳过它继续寻找可领取的任务，同时写入拒付流水（`compute_resource_denials`，含缺口量）作为等待依据，并在任务上标记 `blocked_at`/`block_reason`。
- **结清或释放**：成功按 `metrics.machine_seconds`（缺省取预计值）结算为 `settled`；可重试失败、终态失败、取消以及租约失联恢复都会把本次预留置为 `released`，重新排队后由下一次领取重新预留。
- **加额留痕**：`PUT /api/compute/budgets` 必填 `reason`，每次设置都以差额写入 `compute_budget_adjustments`（理由、操作人、时间）。
- **周期隔离**：周期通过 `POST /api/compute/periods` 显式登记（不可时间窗重叠），未覆盖时间回退到确定性的自然月键 `month-YYYY-MM`。所有流水带周期键，切换学期/月份后旧周期占用不会流入新周期。
- **汇总**：`GET /api/compute/summary?period_key=...` 对每个池分别给出机器时与任务数的 `quota / available / reserved / settled / released / rejected`；全部数字来自确定性聚合，固定时间下重复请求结果一致。拒付明细见 `GET /api/compute/resource-denials`，任务详情内含 `resource_entries` 与 `resource_denials`。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、周期额度池、预留/结算/释放流水、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；资源预留与任务状态翻转同事务提交，成功、失败、取消和失联恢复都会结清或释放预留，超额任务保留拒付流水作为等待依据。租约、额度与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

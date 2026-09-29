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
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

## 学期预算与机器时额度

预算按「学期周期 + 课程 / 班级」两级管理，占用通过只追加的账本（`compute_budget_ledger`）记录，事件分为 `reserve`（预留）、`release`（释放）、`settle`（结算）、`reject`（拒绝）：

- **提交只提示预计消耗**：接口返回 `estimate`（取模板 `max_runtime_seconds` 作为预计机器时、任务数计 1），不占用任何额度。
- **领取时才真正预留**：工作者 `claim` 成功的同一事务内，按课程（及可选班级）写入预留；并发领取导致额度不足时该任务保留排队，不产生半占用。
- **成功**：先释放预计预留，再按 `metrics.machine_seconds`（缺省用本次租约实际耗时）结算机器时，并占用一个任务名额。
- **失败可重试**：释放预计预留并按实际机时结算，任务名额归还；**失败终结**：任务名额一并结算。
- **取消**：排队任务直接取消（从未预留）；运行中任务进入 `cancel_requested`，待工作者回执时结清；等待额度的任务被拒绝时登记 `reject`，保留拒绝理由。
- **失联恢复**：租约过期自动回收，可重试则释放预留（机时照结），重试次数耗尽则按失败终结结算。
- **超额等待**：提交时若课程或班级额度无法覆盖预计消耗，任务进入 `blocked_budget`，`blocked_reason_json` 保存预计量、缺口与等待依据；管理员加额（必须填写理由，留痕于 `compute_budget_adjustments`）后通过 `approve-budget` 放行，仍不足则继续等待。
- **学期切换**：任务、预算与账本均带 `period_key`，关闭旧周期后其中的排队任务不会被新周期领取，新周期从零额度开始，互不串账。
- **月度核对摘要**：`GET /api/compute/budget-summary` 按周期与口径分别给出 `limit / available / reserved / settled / rejected`（机器时与任务数两套），以及等待中的任务数；配合注入时钟，在固定时间重复调用得到完全相同的结果。

主要接口：`POST /api/compute/periods`、`POST /periods/{key}/close`、`PUT /api/compute/budgets`、`POST /api/compute/budgets/adjust`、`GET /api/compute/budgets/adjustments`、`GET /api/compute/blocked-tasks`、`POST /api/compute/tasks/{id}/approve-budget`、`GET /api/compute/budget-summary`。提交任务时通过 `period_key`、`course_code`、`class_code` 归属预算；不带课程编码的任务维持原有配额行为，不占用课程预算。

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from app.core.errors import ValidationError


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def template_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE code=?", (code,)).fetchone()

    def template_by_id(self, template_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE id=?", (template_id,)).fetchone()

    def active_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM compute_templates WHERE active=1 ORDER BY code,version").fetchall()
        return [dict(row) for row in rows]

    def create_template(self, *, code: str, name: str, algorithm: str, parameter_schema: dict[str, Any], defaults: dict[str, Any], max_runtime_seconds: int, max_attempts: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_templates(code,name,algorithm,version,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,1,?,?,?)",
            (code, name, algorithm, json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(defaults, ensure_ascii=False, sort_keys=True), max_runtime_seconds, max_attempts, created_by, now, now),
        )
        return dict(self.template_by_id(cursor.lastrowid))

    def quota(self, subject_type: str, subject_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_quotas WHERE subject_type=? AND subject_key=?", (subject_type, subject_key)).fetchone()

    def upsert_quota(self, *, subject_type: str, subject_key: str, max_queued: int, max_running: int, daily_submissions: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO compute_quotas(subject_type,subject_key,max_queued,max_running,daily_submissions,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_key) DO UPDATE SET max_queued=excluded.max_queued,max_running=excluded.max_running,daily_submissions=excluded.daily_submissions,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (subject_type, subject_key, max_queued, max_running, daily_submissions, actor, now, now),
        )
        return dict(self.quota(subject_type, subject_key))

    def count_user_states(self, requested_by: str) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks WHERE requested_by=? GROUP BY status", (requested_by,)).fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def count_user_submissions_since(self, requested_by: str, since: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE requested_by=? AND created_at>=?", (requested_by, since)).fetchone()[0])

    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.id=?", (task_id,)).fetchone()

    def task_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def create_task(self, *, template_id: int, project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, priority: int, idempotency_key: str, max_attempts: int, period_key: str, course_code: str, class_code: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,period_key,course_code,class_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?)",
            (template_id, project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, period_key, course_code, class_code, now, now),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def create_blocked_task(self, *, template_id: int, project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, priority: int, idempotency_key: str, max_attempts: int, period_key: str, course_code: str, class_code: str, blocked_reason: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,period_key,course_code,class_code,blocked_reason_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'blocked_budget',0,?,?,?,?,?,?,?,?)",
            (template_id, project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, period_key, course_code, class_code, json.dumps(blocked_reason, ensure_ascii=False, sort_keys=True), now, now),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def queued_candidate(self, capabilities: Iterable[str], now: str, period_key: str | None = None) -> sqlite3.Row | None:
        capability_list = sorted(set(capabilities))
        params: list[Any] = [now]
        period_condition = ""
        if period_key:
            period_condition = " AND t.period_key=?"
            params.append(period_key)
        condition = period_condition
        if capability_list:
            placeholders = ",".join("?" for _ in capability_list)
            condition += f" AND tpl.algorithm IN ({placeholders})"
            params.extend(capability_list)
        return self.connection.execute(
            "SELECT t.*,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.status='queued' AND t.available_at<=?" + condition + " ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT 1",
            params,
        ).fetchone()

    def result_versions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_results WHERE task_id=? ORDER BY version", (task_id,)).fetchall()]

    def interventions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_interventions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def add_intervention(self, *, task_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], batch_key: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), batch_key, now),
        )

    def list_tasks(self, *, status: str | None, project_code: str | None, requested_by: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("t.status=?")
            values.append(status)
        if project_code:
            clauses.append("t.project_code=?")
            values.append(project_code)
        if requested_by:
            clauses.append("t.requested_by=?")
            values.append(requested_by)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id" + where + " ORDER BY t.priority DESC,t.created_at DESC,t.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 学期周期 ----

    def create_period(self, *, period_key: str, name: str, starts_at: str, ends_at: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_periods(period_key,name,starts_at,ends_at,status,created_by,created_at,updated_at) VALUES(?,?,?,?,'open',?,?,?)",
            (period_key, name, starts_at, ends_at, created_by, now, now),
        )
        return dict(self.period_by_key(period_key)) if cursor.lastrowid is not None else {}

    def period_by_key(self, period_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_periods WHERE period_key=?", (period_key,)).fetchone()

    def current_period(self, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_periods WHERE status='open' AND starts_at<=? AND ends_at>? ORDER BY starts_at DESC,period_key DESC LIMIT 1",
            (now, now),
        ).fetchone()

    def list_periods(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_periods ORDER BY starts_at DESC,period_key DESC").fetchall()]

    def close_period(self, period_key: str, now: str) -> dict[str, Any] | None:
        self.connection.execute("UPDATE compute_periods SET status='closed',updated_at=? WHERE period_key=?", (now, period_key))
        row = self.period_by_key(period_key)
        return dict(row) if row else None

    # ---- 预算额度 ----

    def budget_by_scope(self, period_key: str, scope_type: str, scope_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_budgets WHERE period_key=? AND scope_type=? AND scope_key=?",
            (period_key, scope_type, scope_key),
        ).fetchone()

    def create_budget(self, *, period_key: str, scope_type: str, scope_key: str, machine_seconds_limit: int, tasks_limit: int, actor: str, reason: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_budgets(period_key,scope_type,scope_key,machine_seconds_limit,tasks_limit,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (period_key, scope_type, scope_key, machine_seconds_limit, tasks_limit, actor, now, now),
        )
        budget_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO compute_budget_adjustments(budget_id,delta_machine_seconds,delta_tasks,reason,actor,created_at) VALUES(?,?,?,?,?,?)",
            (budget_id, machine_seconds_limit, tasks_limit, reason or "初始额度", actor, now),
        )
        return dict(self.budget_by_scope(period_key, scope_type, scope_key))

    def adjust_budget(self, budget: sqlite3.Row, *, delta_machine_seconds: int, delta_tasks: int, reason: str, actor: str, now: str) -> dict[str, Any]:
        new_seconds = int(budget["machine_seconds_limit"]) + delta_machine_seconds
        new_tasks = int(budget["tasks_limit"]) + delta_tasks
        if new_seconds < 0 or new_tasks < 0:
            raise ValidationError("调整后额度不能为负")
        self.connection.execute(
            "UPDATE compute_budgets SET machine_seconds_limit=?,tasks_limit=?,updated_by=?,updated_at=? WHERE id=?",
            (new_seconds, new_tasks, actor, now, budget["id"]),
        )
        self.connection.execute(
            "INSERT INTO compute_budget_adjustments(budget_id,delta_machine_seconds,delta_tasks,reason,actor,created_at) VALUES(?,?,?,?,?,?)",
            (int(budget["id"]), delta_machine_seconds, delta_tasks, reason, actor, now),
        )
        return dict(self.budget_by_scope(budget["period_key"], budget["scope_type"], budget["scope_key"]))

    def list_budget_adjustments(self, budget_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_budget_adjustments WHERE budget_id=? ORDER BY id", (budget_id,)).fetchall()]

    def list_budgets(self, period_key: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM compute_budgets WHERE period_key=? ORDER BY scope_type,scope_key", (period_key,),
        ).fetchall()]

    # ---- 预留与账本 ----

    def open_reserves(self, task_id: int) -> list[sqlite3.Row]:
        """返回该任务尚未被 release/settle 闭合的 reserve 记录（按课程与班级两条线）。"""
        return self.connection.execute(
            "SELECT r.* FROM compute_budget_ledger r WHERE r.task_id=? AND r.event_type='reserve' "
            "AND NOT EXISTS (SELECT 1 FROM compute_budget_ledger c WHERE c.reserve_id=r.id AND c.event_type='release') "
            "ORDER BY r.id",
            (task_id,),
        ).fetchall()

    def ledger_for_task(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_budget_ledger WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def append_ledger(self, *, period_key: str, scope_type: str, scope_key: str, task_id: int | None, attempt_no: int, event_type: str, reserve_id: int | None, machine_seconds: int, tasks_amount: int, reason: str, actor: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO compute_budget_ledger(period_key,scope_type,scope_key,task_id,attempt_no,event_type,reserve_id,machine_seconds,tasks_amount,reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (period_key, scope_type, scope_key, task_id, attempt_no, event_type, reserve_id, machine_seconds, tasks_amount, reason, actor, now),
        )
        return int(cursor.lastrowid)

    def scope_totals(self, period_key: str, scope_type: str, scope_key: str) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN event_type='reserve' THEN machine_seconds ELSE 0 END),0) AS reserved_seconds,"
            "COALESCE(SUM(CASE WHEN event_type='release' THEN machine_seconds ELSE 0 END),0) AS released_seconds,"
            "COALESCE(SUM(CASE WHEN event_type='settle' THEN machine_seconds ELSE 0 END),0) AS settled_seconds,"
            "COALESCE(SUM(CASE WHEN event_type='reject' THEN machine_seconds ELSE 0 END),0) AS rejected_seconds,"
            "COALESCE(SUM(CASE WHEN event_type='reserve' THEN tasks_amount ELSE 0 END),0) AS reserved_tasks,"
            "COALESCE(SUM(CASE WHEN event_type='release' THEN tasks_amount ELSE 0 END),0) AS released_tasks,"
            "COALESCE(SUM(CASE WHEN event_type='settle' THEN tasks_amount ELSE 0 END),0) AS settled_tasks,"
            "COALESCE(SUM(CASE WHEN event_type='reject' THEN tasks_amount ELSE 0 END),0) AS rejected_tasks "
            "FROM compute_budget_ledger WHERE period_key=? AND scope_type=? AND scope_key=?",
            (period_key, scope_type, scope_key),
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def waiting_count(self, period_key: str, scope_type: str, scope_key: str) -> int:
        column = "course_code" if scope_type == "course" else "class_code"
        return int(self.connection.execute(
            f"SELECT COUNT(*) FROM compute_tasks WHERE status='blocked_budget' AND period_key=? AND {column}=?",
            (period_key, scope_key),
        ).fetchone()[0])

    def blocked_tasks(self, *, period_key: str | None = None, scope_type: str | None = None, scope_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["t.status='blocked_budget'"]
        values: list[Any] = []
        if period_key:
            clauses.append("t.period_key=?")
            values.append(period_key)
        if scope_type == "course":
            clauses.append("t.course_code=?")
            values.append(scope_key)
        elif scope_type == "class":
            clauses.append("t.class_code=?")
            values.append(scope_key)
        values.append(limit)
        rows = self.connection.execute(
            "SELECT t.*,tpl.code AS template_code FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE "
            + " AND ".join(clauses) + " ORDER BY t.created_at,t.id LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

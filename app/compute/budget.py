"""资源额度账台：周期、额度池、预留/结算/释放流水与超额拒付记录。

额度在工作者领取任务的一刻才真正预留；提交阶段只做预计消耗提示。
所有流水都带周期键，学期切换后旧周期的占用不会进入新周期的汇总。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from app.core.clock import to_storage
from app.core.errors import ConflictError, ValidationError

PERIOD_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MONTH_PREFIX = "month-"

# 排队候选扫描上限：队首任务额度不足时继续向后寻找可领取的任务。
CLAIM_SCAN_LIMIT = 20

POOL_SUBJECT_TYPES = ("course", "project", "user")


def month_period_key(moment: datetime) -> str:
    """未登记显式周期时的确定性回退：自然月。"""
    return moment.strftime(f"{MONTH_PREFIX}%Y-%m")


def validate_period_key(period_key: str) -> str:
    if not PERIOD_KEY_PATTERN.fullmatch(period_key):
        raise ValidationError("周期键只能包含小写字母、数字、点、下划线和连字符")
    return period_key


class BudgetLedger:
    """在既有 SQLite 连接上读写额度相关表，不自行开启事务。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ------------------------------------------------------------------ 周期

    def resolve_period_key(self, moment: datetime) -> str:
        storage = to_storage(moment)
        row = self.connection.execute(
            "SELECT period_key FROM compute_periods WHERE starts_at<=? AND ends_at>? ORDER BY starts_at DESC,id DESC LIMIT 1",
            (storage, storage),
        ).fetchone()
        if row is not None:
            return str(row["period_key"])
        return month_period_key(moment)

    def register_period(self, *, period_key: str, starts_at: datetime, ends_at: datetime, actor: str, now: str) -> dict[str, Any]:
        validate_period_key(period_key)
        if ends_at <= starts_at:
            raise ValidationError("周期结束时间必须晚于开始时间")
        start_text, end_text = to_storage(starts_at), to_storage(ends_at)
        overlap = self.connection.execute(
            "SELECT period_key FROM compute_periods WHERE starts_at<? AND ends_at>? LIMIT 1",
            (end_text, start_text),
        ).fetchone()
        if overlap is not None:
            raise ConflictError(f"新周期与已登记周期 {overlap['period_key']} 的时间窗重叠")
        try:
            self.connection.execute(
                "INSERT INTO compute_periods(period_key,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (period_key, start_text, end_text, actor, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("周期键已存在") from exc
        return dict(self.period_by_key(period_key))

    def period_by_key(self, period_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_periods WHERE period_key=?", (period_key,)).fetchone()

    def list_periods(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_periods ORDER BY starts_at,id").fetchall()]

    # ---------------------------------------------------------------- 额度池

    def pool(self, period_key: str, subject_type: str, subject_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_budget_pools WHERE period_key=? AND subject_type=? AND subject_key=?",
            (period_key, subject_type, subject_key),
        ).fetchone()

    def matching_pools(self, period_key: str, project_code: str, requested_by: str) -> list[sqlite3.Row]:
        """返回与任务匹配的全部额度池：课程/项目池与个人池同时生效，取交集。"""
        rows = self.connection.execute(
            "SELECT * FROM compute_budget_pools WHERE period_key=? AND ("
            "(subject_type IN ('course','project') AND subject_key=?) OR (subject_type='user' AND subject_key=?)"
            ") ORDER BY id",
            (period_key, project_code, requested_by),
        ).fetchall()
        return list(rows)

    def list_pools(self, period_key: str | None = None) -> list[dict[str, Any]]:
        if period_key is None:
            rows = self.connection.execute("SELECT * FROM compute_budget_pools ORDER BY period_key,id").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM compute_budget_pools WHERE period_key=? ORDER BY id", (period_key,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_pool(
        self, *, period_key: str, subject_type: str, subject_key: str,
        machine_quota: int, task_quota: int, reason: str, actor: str, now: str,
    ) -> dict[str, Any]:
        """管理员设置/临时加额。每次变动必须给出理由，并以差额写入调整流水。"""
        validate_period_key(period_key)
        if subject_type not in POOL_SUBJECT_TYPES:
            raise ValidationError("额度对象类型不合法")
        existing = self.pool(period_key, subject_type, subject_key)
        if existing is None:
            cursor = self.connection.execute(
                "INSERT INTO compute_budget_pools(period_key,subject_type,subject_key,machine_quota,task_quota,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (period_key, subject_type, subject_key, machine_quota, task_quota, actor, actor, now, now),
            )
            pool_id = cursor.lastrowid
            machine_delta, task_delta = machine_quota, task_quota
        else:
            pool_id = int(existing["id"])
            machine_delta = machine_quota - int(existing["machine_quota"])
            task_delta = task_quota - int(existing["task_quota"])
            self.connection.execute(
                "UPDATE compute_budget_pools SET machine_quota=?,task_quota=?,updated_by=?,updated_at=? WHERE id=?",
                (machine_quota, task_quota, actor, now, pool_id),
            )
        self.connection.execute(
            "INSERT INTO compute_budget_adjustments(pool_id,machine_delta,task_delta,reason,actor,created_at) VALUES(?,?,?,?,?,?)",
            (pool_id, machine_delta, task_delta, reason, actor, now),
        )
        return dict(self.pool(period_key, subject_type, subject_key))

    def adjustments(self, pool_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM compute_budget_adjustments WHERE pool_id=? ORDER BY id", (pool_id,)
            ).fetchall()
        ]

    # ------------------------------------------------------------- 占用与汇要

    def pool_totals(self, pool_id: int) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN state='reserved' THEN machine_seconds END),0) AS reserved_machine,"
            "COALESCE(SUM(CASE WHEN state='reserved' THEN task_units END),0) AS reserved_tasks,"
            "COALESCE(SUM(CASE WHEN state='settled' THEN final_machine_seconds END),0) AS settled_machine,"
            "COALESCE(SUM(CASE WHEN state='settled' THEN final_task_units END),0) AS settled_tasks,"
            "COALESCE(SUM(CASE WHEN state='released' THEN machine_seconds END),0) AS released_machine,"
            "COALESCE(SUM(CASE WHEN state='released' THEN task_units END),0) AS released_tasks "
            "FROM compute_resource_entries WHERE pool_id=?",
            (pool_id,),
        ).fetchone()
        return {key: int(value) for key, value in dict(row).items()}

    def availability(self, pool: sqlite3.Row, *, machine_required: int, tasks_required: int) -> dict[str, int]:
        totals = self.pool_totals(int(pool["id"]))
        available_machine = int(pool["machine_quota"]) - totals["reserved_machine"] - totals["settled_machine"]
        available_tasks = int(pool["task_quota"]) - totals["reserved_tasks"] - totals["settled_tasks"]
        return {
            "pool_id": int(pool["id"]),
            "available_machine_seconds": available_machine,
            "available_tasks": available_tasks,
            "machine_shortfall": max(0, machine_required - available_machine),
            "task_shortfall": max(0, tasks_required - available_tasks),
        }

    def reserve(
        self, *, period_key: str, task_id: int, attempt_no: int,
        machine_seconds: int, task_units: int, pools: Iterable[sqlite3.Row], now: str,
    ) -> int:
        count = 0
        for pool in pools:
            self.connection.execute(
                "INSERT INTO compute_resource_entries(period_key,subject_type,subject_key,pool_id,task_id,attempt_no,machine_seconds,task_units,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,'reserved',?,?)",
                (period_key, pool["subject_type"], pool["subject_key"], pool["id"], task_id, attempt_no, machine_seconds, task_units, now, now),
            )
            count += 1
        return count

    def settle(self, *, task_id: int, attempt_no: int, machine_seconds: int, task_units: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE compute_resource_entries SET state='settled',final_machine_seconds=?,final_task_units=?,settled_at=?,updated_at=?"
            " WHERE task_id=? AND attempt_no=? AND state='reserved'",
            (machine_seconds, task_units, now, now, task_id, attempt_no),
        )
        return cursor.rowcount

    def release(self, *, task_id: int, attempt_no: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE compute_resource_entries SET state='released',released_at=?,updated_at=?"
            " WHERE task_id=? AND attempt_no=? AND state='reserved'",
            (now, now, task_id, attempt_no),
        )
        return cursor.rowcount

    # ------------------------------------------------------------- 超额拒付记录

    def record_denial(
        self, *, period_key: str, pool: sqlite3.Row, task_id: int, attempt_no: int,
        machine_required: int, tasks_required: int, available_machine: int, available_tasks: int, now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO compute_resource_denials(period_key,subject_type,subject_key,pool_id,task_id,attempt_no,"
            "required_machine_seconds,required_tasks,available_machine_seconds,available_tasks,first_denied_at,last_denied_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(pool_id,task_id,attempt_no) DO UPDATE SET required_machine_seconds=excluded.required_machine_seconds,"
            "required_tasks=excluded.required_tasks,available_machine_seconds=excluded.available_machine_seconds,"
            "available_tasks=excluded.available_tasks,last_denied_at=excluded.last_denied_at",
            (period_key, pool["subject_type"], pool["subject_key"], pool["id"], task_id, attempt_no,
             machine_required, tasks_required, available_machine, available_tasks, now, now),
        )

    def resolve_denials(self, *, task_id: int, attempt_no: int | None, resolution: str, now: str) -> int:
        if attempt_no is None:
            cursor = self.connection.execute(
                "UPDATE compute_resource_denials SET resolved_at=?,resolution=? WHERE task_id=? AND resolved_at=''",
                (now, resolution, task_id),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE compute_resource_denials SET resolved_at=?,resolution=? WHERE task_id=? AND attempt_no=? AND resolved_at=''",
                (now, resolution, task_id, attempt_no),
            )
        return cursor.rowcount

    def open_denial_totals(self, pool_id: int) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS denial_count,"
            "COALESCE(SUM(required_machine_seconds),0) AS rejected_machine,"
            "COALESCE(SUM(required_tasks),0) AS rejected_tasks "
            "FROM compute_resource_denials WHERE pool_id=? AND resolved_at=''",
            (pool_id,),
        ).fetchone()
        return {key: int(value) for key, value in dict(row).items()}

    def denials(self, *, period_key: str | None = None, task_id: int | None = None, include_resolved: bool = True) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if period_key is not None:
            clauses.append("period_key=?")
            values.append(period_key)
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if not include_resolved:
            clauses.append("resolved_at=''")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(500)
        rows = self.connection.execute(
            "SELECT * FROM compute_resource_denials" + where + " ORDER BY id DESC LIMIT ?", values
        ).fetchall()
        return [dict(row) for row in rows]

    def entries(self, task_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM compute_resource_entries WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ]

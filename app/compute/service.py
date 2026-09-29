from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.compute.repository import ComputeRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

MACHINE_SECONDS_METRIC_KEYS = ("machine_seconds", "machine_time_seconds")


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class ComputeOperationsService:
    """管理计算模板、配额、任务租约、结果版本、学期预算和人工干预。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ComputeRepository(self.connection)

    def list_templates(self) -> list[dict[str, Any]]:
        return self.repository.active_templates()

    def create_template(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            if repository.template_by_code(payload["code"]):
                raise ConflictError("参数模板编码已存在")
            return repository.create_template(
                code=payload["code"], name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                created_by=actor, now=now,
            )

    def set_quota(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return ComputeRepository(connection).upsert_quota(actor=actor, now=now, **payload)

    # ---- 学期周期 ----

    def create_period(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        starts_at = payload["starts_at"]
        ends_at = payload["ends_at"]
        if ends_at <= starts_at:
            raise ValidationError("周期结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        start_text = to_storage(starts_at)
        end_text = to_storage(ends_at)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            if repository.period_by_key(payload["period_key"]):
                raise ConflictError("学期周期编码已存在")
            return repository.create_period(
                period_key=payload["period_key"], name=payload["name"],
                starts_at=start_text, ends_at=end_text, created_by=actor, now=now,
            )

    def list_periods(self) -> list[dict[str, Any]]:
        return self.repository.list_periods()

    def close_period(self, period_key: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            period = repository.period_by_key(period_key)
            if period is None:
                raise NotFoundError("学期周期不存在")
            if period["status"] == "closed":
                raise ConflictError("学期周期已经关闭")
            return repository.close_period(period_key, now)

    def _resolve_period(self, repository: ComputeRepository, now_value: datetime, period_key: str | None, *, require_open: bool = True) -> sqlite3.Row:
        now = to_storage(now_value)
        if period_key:
            period = repository.period_by_key(period_key)
            if period is None:
                raise NotFoundError("学期周期不存在")
        else:
            period = repository.current_period(now)
            if period is None:
                period = repository.period_by_key("default")
            if period is None:
                raise ConflictError("当前没有开放的学期周期，请先创建周期或在提交时指定 period_key")
        if require_open and period["status"] != "open":
            raise ConflictError("学期周期已关闭，不能继续提交或领取任务")
        return period

    # ---- 预算额度 ----

    def set_budget(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            period = self._resolve_period(repository, now_value, payload.get("period_key"))
            existing = repository.budget_by_scope(period["period_key"], payload["scope_type"], payload["scope_key"])
            if existing is not None:
                raise ConflictError("额度已经存在，请使用加额/调额接口变更，并填写理由")
            return repository.create_budget(
                period_key=period["period_key"], scope_type=payload["scope_type"], scope_key=payload["scope_key"],
                machine_seconds_limit=payload["machine_seconds_limit"], tasks_limit=payload["tasks_limit"],
                actor=actor, reason=payload.get("reason") or "初始额度", now=now,
            )

    def adjust_budget(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        reason = (payload.get("reason") or "").strip()
        if len(reason) < 2:
            raise ValidationError("管理员临时加额/调额必须填写理由")
        if payload["delta_machine_seconds"] == 0 and payload["delta_tasks"] == 0:
            raise ValidationError("调整数量不能全部为零")
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            period = self._resolve_period(repository, now_value, payload.get("period_key"))
            budget = repository.budget_by_scope(period["period_key"], payload["scope_type"], payload["scope_key"])
            if budget is None:
                raise NotFoundError("额度不存在，请先初始化课程或班级额度")
            return repository.adjust_budget(
                budget, delta_machine_seconds=payload["delta_machine_seconds"], delta_tasks=payload["delta_tasks"],
                reason=reason, actor=actor, now=now,
            )

    def list_budgets(self, period_key: str | None = None) -> list[dict[str, Any]]:
        now_value = self.clock.now()
        with transaction(immediate=False) as connection:
            repository = ComputeRepository(connection)
            period = self._resolve_period(repository, now_value, period_key, require_open=False)
            return repository.list_budgets(period["period_key"])

    def list_budget_adjustments(self, period_key: str, scope_type: str, scope_key: str) -> list[dict[str, Any]]:
        with transaction(immediate=False) as connection:
            repository = ComputeRepository(connection)
            budget = repository.budget_by_scope(period_key, scope_type, scope_key)
            if budget is None:
                raise NotFoundError("额度不存在")
            return repository.list_budget_adjustments(int(budget["id"]))

    # ---- 提交：只给预计消耗，不占用额度 ----

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = repository.template_by_code(payload["template_code"])
            if template is None or not template["active"]:
                raise NotFoundError("参数模板不存在或已经停用")
            parameters = self._validate_parameters(template, payload["parameters"])
            existing = repository.task_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            parameter_digest = digest(parameters)
            if existing is not None:
                if existing["parameter_digest"] != parameter_digest:
                    raise ConflictError("同一幂等键对应了不同的计算参数")
                return dict(repository.task_by_id(existing["id"]))
            self._check_quota(repository, payload["requested_by"], now_value)
            period = self._resolve_period(repository, now_value, payload.get("period_key"))
            course_code = (payload.get("course_code") or "").strip()
            class_code = (payload.get("class_code") or "").strip()
            estimate = self._estimate(template)
            task_values = dict(
                template_id=template["id"], project_code=payload["project_code"],
                requested_by=payload["requested_by"], parameters=parameters,
                parameter_digest=parameter_digest, priority=payload["priority"],
                idempotency_key=payload["idempotency_key"], max_attempts=template["max_attempts"],
                period_key=period["period_key"], course_code=course_code, class_code=class_code, now=now,
            )
            if not course_code:
                # 未纳入课程/班级额度管理的提交沿用旧行为：排队等待领取。
                task = repository.create_task(**task_values)
            else:
                shortages = self._budget_shortages(repository, period["period_key"], course_code, class_code, estimate["machine_seconds"], estimate["tasks"])
                if shortages:
                    blocked_reason = {
                        "at": now,
                        "period_key": period["period_key"],
                        "estimate": estimate,
                        "shortages": shortages,
                        "waiting": "预算不足，等待管理员加额或已有预留释放后审批放行",
                    }
                    task = repository.create_blocked_task(blocked_reason=blocked_reason, **task_values)
                else:
                    task = repository.create_task(**task_values)
            task["estimate"] = estimate
            return task

    def list_tasks(self, *, status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=max(1, min(limit, 500)))

    def list_blocked_tasks(self, period_key: str | None = None, scope_type: str | None = None, scope_key: str | None = None) -> list[dict[str, Any]]:
        return self.repository.blocked_tasks(period_key=period_key, scope_type=scope_type, scope_key=scope_key)

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        result = dict(row)
        result["results"] = self.repository.result_versions(task_id)
        result["interventions"] = self.repository.interventions(task_id)
        result["budget_ledger"] = self.repository.ledger_for_task(task_id)
        return result

    # ---- 领取：额度在这一刻真正预留 ----

    def claim(self, worker_id: str, capabilities: list[str], lease_seconds: int) -> dict[str, Any] | None:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            period = self._resolve_period(repository, now_value, None)
            candidate = repository.queued_candidate(capabilities, now, period["period_key"])
            if candidate is None:
                return None
            template = repository.template_by_id(candidate["template_id"])
            estimate = self._estimate(template)
            shortages = self._budget_shortages(repository, period["period_key"], candidate["course_code"], candidate["class_code"], estimate["machine_seconds"], estimate["tasks"])
            if shortages:
                # 并发领取导致额度被占满：保留排队，工作者稍后重试，不制造半占用。
                return None
            cursor = connection.execute(
                "UPDATE compute_tasks SET status='running',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,started_at=?,updated_at=?,version=version+1 WHERE id=? AND status='queued'",
                (worker_id, lease_until, now, now, candidate["id"]),
            )
            if cursor.rowcount != 1:
                return None
            task = dict(repository.task_by_id(candidate["id"]))
            self._reserve(repository, task, estimate["machine_seconds"], estimate["tasks"], worker_id, now)
            task["reservation"] = estimate
            return task

    def heartbeat(self, task_id: int, worker_id: str, lease_seconds: int) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE compute_tasks SET lease_expires_at=?,updated_at=?,version=version+1 WHERE id=? AND status='running' AND lease_owner=?",
                (expires, now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("任务未由当前工作者持有")
            return dict(ComputeRepository(connection).task_by_id(task_id))

    # ---- 成功：释放预留并按实际占用结算 ----

    def complete(self, task_id: int, worker_id: str, result: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] not in {"running", "cancel_requested"} or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            actual_seconds = self._actual_machine_seconds(task, metrics, now_value)
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_results WHERE task_id=?", (task_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO compute_results(task_id,version,result_json,metrics_json,result_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, version, json.dumps(result, ensure_ascii=False, sort_keys=True), json.dumps(metrics, ensure_ascii=False, sort_keys=True), digest({"result": result, "metrics": metrics}), worker_id, now),
            )
            cancel_pending = task["status"] == "cancel_requested"
            final_status = "cancelled" if cancel_pending else "succeeded"
            connection.execute(
                "UPDATE compute_tasks SET status=?,current_result_version=?,lease_owner='',lease_expires_at='',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (final_status, version, now, now, task_id),
            )
            self._close_attempt(repository, task, actual_seconds=actual_seconds, terminal=True, settle_tasks=not cancel_pending, reason="cancelled_by_admin" if cancel_pending else "succeeded", actor=worker_id, now=now)
            return dict(repository.task_by_id(task_id))

    # ---- 失败：可重试则释放本次预留，终结则同时结算 ----

    def fail(self, task_id: int, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] not in {"running", "cancel_requested"} or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            cancel_pending = task["status"] == "cancel_requested"
            can_retry = (not cancel_pending) and retryable and int(task["attempt_count"]) < int(task["max_attempts"])
            status = "queued" if can_retry else ("cancelled" if cancel_pending else "failed")
            delay = min(300, 2 ** max(0, int(task["attempt_count"]) - 1)) if can_retry else 0
            available = to_storage(now_value + timedelta(seconds=delay))
            actual_seconds = self._actual_machine_seconds(task, {}, now_value)
            connection.execute(
                "UPDATE compute_tasks SET status=?,available_at=?,lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (status, available, error_code, message[:2000], None if can_retry else now, now, task_id),
            )
            self._close_attempt(repository, task, actual_seconds=actual_seconds, terminal=not can_retry, settle_tasks=not can_retry and not cancel_pending, reason="cancelled_by_admin" if cancel_pending else "failed", actor=worker_id, now=now)
            return dict(repository.task_by_id(task_id))

    def cancel(self, task_id: int, actor: str, reason: str, batch_key: str = "") -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            before = dict(task)
            if task["status"] == "blocked_budget":
                # 管理员拒绝等待中的超额任务：登记为被拒绝数量，留下等待依据与拒绝理由。
                connection.execute(
                    "UPDATE compute_tasks SET status='cancelled',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (now, now, task_id),
                )
                estimate = self._estimate(repository.template_by_id(task["template_id"]))
                self._append_scopes(
                    repository, task, event_type="reject", machine_seconds=estimate["machine_seconds"],
                    tasks_amount=estimate["tasks"], reason=f"拒绝等待任务：{reason}", actor=actor, now=now,
                    attempt_no=int(task["attempt_count"]),
                )
            elif task["status"] == "queued":
                # 尚未领取，没有任何预留，直接取消即可。
                connection.execute(
                    "UPDATE compute_tasks SET status='cancelled',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (now, now, task_id),
                )
            elif task["status"] == "running":
                connection.execute(
                    "UPDATE compute_tasks SET status='cancel_requested',updated_at=?,version=version+1 WHERE id=?",
                    (now, task_id),
                )
            else:
                raise ConflictError("当前任务状态不允许取消")
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(task_id=task_id, actor=actor, action="cancel", reason=reason, before=before, after=after, batch_key=batch_key, now=now)
            return after

    def retry(self, task_id: int, actor: str, reason: str, priority: int | None = None, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"failed", "cancelled"}:
                raise ConflictError("只有失败或已取消任务可以人工重试")
            estimate = self._estimate(ComputeRepository(connection).template_by_id(task["template_id"]))
            shortages = self._budget_shortages(ComputeRepository(connection), task["period_key"], task["course_code"], task["class_code"], estimate["machine_seconds"], estimate["tasks"])
            if shortages:
                raise ConflictError("课程或班级额度不足，任务需继续等待加额", context={"shortages": shortages})
            chosen = task["priority"] if priority is None else priority
            connection.execute("UPDATE compute_tasks SET status='queued',priority=?,available_at=?,lease_owner='',lease_expires_at='',finished_at=NULL,blocked_reason_json='',updated_at=?,version=version+1 WHERE id=?", (chosen, now, now, task["id"]))
        return self._intervene(task_id, actor, reason, "retry", batch_key, mutate)

    def approve_blocked_task(self, task_id: int, actor: str, reason: str) -> dict[str, Any]:
        """管理员对照等待依据审批：额度够则放行排队，仍不足则保留等待状态。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "blocked_budget":
                raise ConflictError("只有等待额度的任务可以审批放行")
            before = dict(task)
            estimate = self._estimate(repository.template_by_id(task["template_id"]))
            shortages = self._budget_shortages(repository, task["period_key"], task["course_code"], task["class_code"], estimate["machine_seconds"], estimate["tasks"])
            if shortages:
                raise ConflictError("额度仍然不足，任务继续等待", context={"shortages": shortages})
            connection.execute(
                "UPDATE compute_tasks SET status='queued',blocked_reason_json='',available_at=?,updated_at=?,version=version+1 WHERE id=?",
                (now, now, task_id),
            )
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(task_id=task_id, actor=actor, action="budget_approval", reason=reason, before=before, after=after, batch_key="", now=now)
            return after

    def set_priority(self, task_id: int, actor: str, reason: str, priority: int, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"queued", "running"}:
                raise ConflictError("只有排队或运行中的任务可以调整优先级")
            connection.execute("UPDATE compute_tasks SET priority=?,updated_at=?,version=version+1 WHERE id=?", (priority, now, task["id"]))
        return self._intervene(task_id, actor, reason, "priority", batch_key, mutate)

    def batch_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_key = digest({"actor": payload["actor"], "task_ids": payload["task_ids"], "operation": payload["operation"], "reason": payload["reason"]})
        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for task_id in list(dict.fromkeys(payload["task_ids"])):
            try:
                if payload["operation"] == "cancel":
                    value = self.cancel(task_id, payload["actor"], payload["reason"], batch_key)
                elif payload["operation"] == "retry":
                    value = self.retry(task_id, payload["actor"], payload["reason"], payload.get("priority"), batch_key)
                else:
                    value = self.set_priority(task_id, payload["actor"], payload["reason"], int(payload["priority"]), batch_key)
                succeeded.append({"task_id": task_id, "status": value["status"], "version": value["version"]})
            except (ConflictError, NotFoundError) as exc:
                failed.append({"task_id": task_id, "code": exc.code, "message": exc.message})
        return {"batch_key": batch_key, "succeeded": succeeded, "failed": failed}

    # ---- 失联恢复：预留随恢复路径结清或留给下次领取 ----

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        recovered: list[int] = []
        exhausted: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            rows = connection.execute("SELECT * FROM compute_tasks WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id", (now,)).fetchall()
            for task in rows:
                before = dict(task)
                if int(task["attempt_count"]) < int(task["max_attempts"]):
                    status, finished_at = "queued", None
                    recovered.append(int(task["id"]))
                    terminal = False
                else:
                    status, finished_at = "failed", now
                    exhausted.append(int(task["id"]))
                    terminal = True
                actual_seconds = self._actual_machine_seconds(task, {}, now_value)
                connection.execute(
                    "UPDATE compute_tasks SET status=?,lease_owner='',lease_expires_at='',available_at=?,last_error_code='lease_expired',last_error_message='工作者租约已过期',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (status, now, finished_at, now, task["id"]),
                )
                self._close_attempt(repository, task, actual_seconds=actual_seconds, terminal=terminal, reason="lease_expired", actor=actor, now=now)
                after = dict(repository.task_by_id(task["id"]))
                repository.add_intervention(task_id=task["id"], actor=actor, action="lease_recovery", reason="租约过期自动恢复", before=before, after=after, batch_key="", now=now)
        return {"recovered": recovered, "exhausted": exhausted}

    # ---- 摘要：可用 / 已预留 / 已结算 / 被拒绝，按学期与口径互不串账 ----

    def budget_summary(self, period_key: str | None = None, scope_type: str | None = None, scope_key: str | None = None) -> dict[str, Any]:
        now_value = self.clock.now()
        with transaction(immediate=False) as connection:
            repository = ComputeRepository(connection)
            period = self._resolve_period(repository, now_value, period_key, require_open=False)
            budgets = repository.list_budgets(period["period_key"])
            items: list[dict[str, Any]] = []
            for budget in budgets:
                if scope_type and budget["scope_type"] != scope_type:
                    continue
                if scope_key and budget["scope_key"] != scope_key:
                    continue
                totals = repository.scope_totals(period["period_key"], budget["scope_type"], budget["scope_key"])
                reserved_seconds = max(0, totals["reserved_seconds"] - totals["released_seconds"])
                reserved_tasks = max(0, totals["reserved_tasks"] - totals["released_tasks"])
                settled_seconds = totals["settled_seconds"]
                settled_tasks = totals["settled_tasks"]
                waiting = repository.waiting_count(period["period_key"], budget["scope_type"], budget["scope_key"])
                items.append({
                    "scope_type": budget["scope_type"],
                    "scope_key": budget["scope_key"],
                    "machine_seconds": {
                        "limit": int(budget["machine_seconds_limit"]),
                        "reserved": reserved_seconds,
                        "settled": settled_seconds,
                        "rejected": totals["rejected_seconds"],
                        "available": int(budget["machine_seconds_limit"]) - reserved_seconds - settled_seconds,
                    },
                    "tasks": {
                        "limit": int(budget["tasks_limit"]),
                        "reserved": reserved_tasks,
                        "settled": settled_tasks,
                        "rejected": totals["rejected_tasks"],
                        "available": int(budget["tasks_limit"]) - reserved_tasks - settled_tasks,
                    },
                    "waiting_tasks": waiting,
                })
            return {"period_key": period["period_key"], "period_name": period["name"], "as_of": to_storage(now_value), "items": items}

    def summary(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks GROUP BY status ORDER BY status").fetchall()
        oldest = self.connection.execute("SELECT MIN(created_at) FROM compute_tasks WHERE status='queued'").fetchone()[0]
        return {"states": {row["status"]: row["amount"] for row in rows}, "oldest_queued_at": oldest, "templates": len(self.repository.active_templates())}

    # ---- 内部辅助 ----

    @staticmethod
    def _estimate(template: sqlite3.Row) -> dict[str, int]:
        return {"machine_seconds": int(template["max_runtime_seconds"]), "tasks": 1}

    @staticmethod
    def _actual_machine_seconds(task: sqlite3.Row, metrics: dict[str, Any], now_value: datetime) -> int:
        for key in MACHINE_SECONDS_METRIC_KEYS:
            value = metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                return int(value)
        started = task["started_at"]
        if started:
            start_dt = datetime.fromisoformat(started)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            return max(0, int((now_value - start_dt.astimezone(UTC)).total_seconds()))
        return 0

    def _budget_shortages(self, repository: ComputeRepository, period_key: str, course_code: str, class_code: str, need_seconds: int, need_tasks: int) -> list[dict[str, Any]]:
        if not course_code:
            return []
        course_budget = repository.budget_by_scope(period_key, "course", course_code)
        if course_budget is None:
            raise ConflictError(f"学期 {period_key} 内未配置课程 {course_code} 的额度，请联系管理员初始化")
        scopes = [("course", course_code, course_budget)]
        if class_code:
            class_budget = repository.budget_by_scope(period_key, "class", class_code)
            if class_budget is not None:
                scopes.append(("class", class_code, class_budget))
        shortages: list[dict[str, Any]] = []
        for scope_type, scope_key, budget in scopes:
            totals = repository.scope_totals(period_key, scope_type, scope_key)
            reserved_seconds = max(0, totals["reserved_seconds"] - totals["released_seconds"])
            reserved_tasks = max(0, totals["reserved_tasks"] - totals["released_tasks"])
            available_seconds = int(budget["machine_seconds_limit"]) - reserved_seconds - totals["settled_seconds"]
            available_tasks = int(budget["tasks_limit"]) - reserved_tasks - totals["settled_tasks"]
            shortage_seconds = max(0, need_seconds - available_seconds)
            shortage_tasks = max(0, need_tasks - available_tasks)
            if shortage_seconds or shortage_tasks:
                shortages.append({
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "available_machine_seconds": available_seconds,
                    "available_tasks": available_tasks,
                    "need_machine_seconds": need_seconds,
                    "need_tasks": need_tasks,
                    "shortage_machine_seconds": shortage_seconds,
                    "shortage_tasks": shortage_tasks,
                })
        return shortages

    def _reserve(self, repository: ComputeRepository, task: sqlite3.Row | dict[str, Any], machine_seconds: int, tasks_amount: int, actor: str, now: str) -> None:
        if not task["course_code"]:
            return
        self._append_scopes(repository, task, event_type="reserve", machine_seconds=machine_seconds, tasks_amount=tasks_amount, reason="worker_claim", actor=actor, now=now, attempt_no=int(task["attempt_count"]))

    def _close_attempt(self, repository: ComputeRepository, task: sqlite3.Row, *, actual_seconds: int, terminal: bool, reason: str, actor: str, now: str, settle_tasks: bool | None = None) -> None:
        if not task["course_code"]:
            return
        settle_task_slot = terminal if settle_tasks is None else settle_tasks
        for reserve in repository.open_reserves(int(task["id"])):
            # 先释放本次预留（预计机器时与任务位），再按实际机器时结算。
            repository.append_ledger(
                period_key=reserve["period_key"], scope_type=reserve["scope_type"], scope_key=reserve["scope_key"],
                task_id=int(task["id"]), attempt_no=int(reserve["attempt_no"]), event_type="release",
                reserve_id=int(reserve["id"]), machine_seconds=int(reserve["machine_seconds"]),
                tasks_amount=int(reserve["tasks_amount"]), reason=reason, actor=actor, now=now,
            )
            repository.append_ledger(
                period_key=reserve["period_key"], scope_type=reserve["scope_type"], scope_key=reserve["scope_key"],
                task_id=int(task["id"]), attempt_no=int(reserve["attempt_no"]), event_type="settle",
                reserve_id=int(reserve["id"]), machine_seconds=actual_seconds,
                tasks_amount=1 if settle_task_slot else 0, reason=reason, actor=actor, now=now,
            )

    def _append_scopes(self, repository: ComputeRepository, task: sqlite3.Row | dict[str, Any], *, event_type: str, machine_seconds: int, tasks_amount: int, reason: str, actor: str, now: str, attempt_no: int) -> None:
        scopes = [("course", task["course_code"])]
        if task["class_code"]:
            if repository.budget_by_scope(task["period_key"], "class", task["class_code"]) is not None:
                scopes.append(("class", task["class_code"]))
        for scope_type, scope_key in scopes:
            repository.append_ledger(
                period_key=task["period_key"], scope_type=scope_type, scope_key=scope_key,
                task_id=int(task["id"]), attempt_no=attempt_no, event_type=event_type, reserve_id=None,
                machine_seconds=machine_seconds, tasks_amount=tasks_amount, reason=reason, actor=actor, now=now,
            )

    def _intervene(self, task_id: int, actor: str, reason: str, action: str, batch_key: str, mutation: Callable[[sqlite3.Connection, sqlite3.Row, str], None]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            before = dict(task)
            mutation(connection, task, now)
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(task_id=task_id, actor=actor, action=action, reason=reason, before=before, after=after, batch_key=batch_key, now=now)
            return after

    def _check_quota(self, repository: ComputeRepository, requested_by: str, now: datetime) -> None:
        quota = repository.quota("user", requested_by)
        if quota is None:
            return
        states = repository.count_user_states(requested_by)
        if states.get("queued", 0) + states.get("blocked_budget", 0) >= int(quota["max_queued"]):
            raise ConflictError("用户排队任务配额已用尽")
        if states.get("running", 0) >= int(quota["max_running"]):
            raise ConflictError("用户运行任务配额已用尽")
        day_start = to_storage(now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
        if repository.count_user_submissions_since(requested_by, day_start) >= int(quota["daily_submissions"]):
            raise ConflictError("用户当日提交配额已用尽")

    @staticmethod
    def _validate_schema(schema: dict[str, dict[str, Any]], defaults: dict[str, Any]) -> None:
        if not schema:
            raise ValidationError("参数模板至少包含一个参数")
        allowed = {"integer", "number", "string", "boolean"}
        for name, rule in schema.items():
            if not name or not isinstance(rule, dict) or rule.get("type") not in allowed:
                raise ValidationError(f"参数 {name or '<empty>'} 的规则不合法")
        if set(defaults) - set(schema):
            raise ValidationError("默认值包含未声明参数")

    def _validate_parameters(self, template: sqlite3.Row, supplied: dict[str, Any]) -> dict[str, Any]:
        schema = json.loads(template["parameter_schema_json"])
        values = {**json.loads(template["default_parameters_json"]), **supplied}
        unknown = set(values) - set(schema)
        if unknown:
            raise ValidationError("包含模板未声明的参数", context={"parameters": sorted(unknown)})
        normalized: dict[str, Any] = {}
        for name, rule in schema.items():
            if name not in values:
                if rule.get("required"):
                    raise ValidationError(f"缺少必填参数：{name}")
                continue
            value = values[name]
            kind = rule["type"]
            valid = {"integer": isinstance(value, int) and not isinstance(value, bool), "number": isinstance(value, (int, float)) and not isinstance(value, bool), "string": isinstance(value, str), "boolean": isinstance(value, bool)}[kind]
            if not valid:
                raise ValidationError(f"参数 {name} 类型不正确")
            if rule.get("minimum") is not None and value < rule["minimum"]:
                raise ValidationError(f"参数 {name} 小于允许的最小值")
            if rule.get("maximum") is not None and value > rule["maximum"]:
                raise ValidationError(f"参数 {name} 大于允许的最大值")
            if rule.get("choices") and value not in rule["choices"]:
                raise ValidationError(f"参数 {name} 不在允许的选项中")
            normalized[name] = value
        return normalized

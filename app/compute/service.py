from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.compute.budget import CLAIM_SCAN_LIMIT, BudgetLedger, month_period_key, validate_period_key
from app.compute.repository import ComputeRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class ComputeOperationsService:
    """管理计算模板、资源额度、任务租约、结果版本和人工干预。

    资源额度（机器时、任务数）在工作者领取任务时才真正预留：提交只返回预计
    消耗；成功时按实际用量结算，失败、取消与租约恢复都会结清或释放预留。
    所有占用都带周期键，学期/月份切换后旧周期占用不会流入新周期。
    """

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
                estimated_machine_seconds=payload.get("estimated_machine_seconds", 600),
                created_by=actor, now=now,
            )

    # ------------------------------------------------------------- 周期与额度

    def register_period(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return BudgetLedger(connection).register_period(
                period_key=payload["period_key"], starts_at=payload["starts_at"],
                ends_at=payload["ends_at"], actor=actor, now=now,
            )

    def list_periods(self) -> list[dict[str, Any]]:
        return BudgetLedger(self.connection).list_periods()

    def set_budget(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """管理员设置或临时调整额度。reason 必填，每次变动留下差额与理由。"""
        reason = (payload.get("reason") or "").strip()
        if len(reason) < 2:
            raise ValidationError("调整额度必须填写理由")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return BudgetLedger(connection).upsert_pool(
                period_key=payload["period_key"], subject_type=payload["subject_type"],
                subject_key=payload["subject_key"], machine_quota=payload["machine_seconds_quota"],
                task_quota=payload["task_quota"], reason=reason, actor=actor, now=now,
            )

    def list_budgets(self, period_key: str | None = None) -> list[dict[str, Any]]:
        return BudgetLedger(self.connection).list_pools(period_key)

    def list_denials(self, *, period_key: str | None = None, task_id: int | None = None, include_resolved: bool = True) -> list[dict[str, Any]]:
        return BudgetLedger(self.connection).denials(period_key=period_key, task_id=task_id, include_resolved=include_resolved)

    # ----------------------------------------------------------------- 提交

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
                task = dict(repository.task_by_id(existing["id"]))
            else:
                # 提交阶段不占用任何额度，只记录预计消耗供提示与领取时预留。
                task = repository.create_task(
                    template_id=template["id"], project_code=payload["project_code"],
                    requested_by=payload["requested_by"], parameters=parameters,
                    parameter_digest=parameter_digest, priority=payload["priority"],
                    idempotency_key=payload["idempotency_key"], max_attempts=template["max_attempts"],
                    estimated_machine_seconds=int(template["estimated_machine_seconds"]), now=now,
                )
            ledger = BudgetLedger(connection)
            task["resource_preview"] = {
                "period_key": ledger.resolve_period_key(now_value),
                "estimated_machine_seconds": int(task["estimated_machine_seconds"]),
                "estimated_tasks": 1,
                "notice": "以上为预计消耗，实际额度在工作者领取任务时才会预留",
            }
            return task

    def list_tasks(self, *, status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=max(1, min(limit, 500)))

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        result = dict(row)
        result["results"] = self.repository.result_versions(task_id)
        result["interventions"] = self.repository.interventions(task_id)
        ledger = BudgetLedger(self.connection)
        result["resource_entries"] = ledger.entries(task_id)
        result["resource_denials"] = ledger.denials(task_id=task_id, include_resolved=True)
        return result

    # ----------------------------------------------------------------- 领取

    def claim(self, worker_id: str, capabilities: list[str], lease_seconds: int) -> dict[str, Any] | None:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            ledger = BudgetLedger(connection)
            period_key = ledger.resolve_period_key(now_value)
            candidates = repository.queued_candidates(capabilities, now, CLAIM_SCAN_LIMIT)
            for candidate in candidates:
                pools = ledger.matching_pools(period_key, candidate["project_code"], candidate["requested_by"])
                required_machine = max(1, int(candidate["estimated_machine_seconds"]))
                required_tasks = 1
                shortages = []
                blocking_pools: list[sqlite3.Row] = []
                for pool in pools:
                    availability = ledger.availability(pool, machine_required=required_machine, tasks_required=required_tasks)
                    if availability["machine_shortfall"] or availability["task_shortfall"]:
                        shortages.append({
                            "subject_type": pool["subject_type"], "subject_key": pool["subject_key"],
                            **availability,
                        })
                        blocking_pools.append(pool)
                if blocking_pools:
                    # 超额任务保留排队与等待依据：记录拒付流水，继续尝试后面的候选。
                    attempt_no = int(candidate["attempt_count"]) + 1
                    for pool, availability in zip(blocking_pools, shortages):
                        ledger.record_denial(
                            period_key=period_key, pool=pool, task_id=int(candidate["id"]), attempt_no=attempt_no,
                            machine_required=required_machine, tasks_required=required_tasks,
                            available_machine=availability["available_machine_seconds"],
                            available_tasks=availability["available_tasks"], now=now,
                        )
                    reason = json.dumps({"period_key": period_key, "shortages": shortages}, ensure_ascii=False, sort_keys=True)[:2000]
                    repository.mark_task_blocked(int(candidate["id"]), reason, now)
                    continue
                cursor = connection.execute(
                    "UPDATE compute_tasks SET status='running',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),blocked_at='',block_reason='',updated_at=?,version=version+1 WHERE id=? AND status='queued'",
                    (worker_id, lease_until, now, now, candidate["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                attempt_no = int(candidate["attempt_count"]) + 1
                ledger.reserve(
                    period_key=period_key, task_id=int(candidate["id"]), attempt_no=attempt_no,
                    machine_seconds=required_machine, task_units=required_tasks, pools=pools, now=now,
                )
                ledger.resolve_denials(task_id=int(candidate["id"]), attempt_no=attempt_no, resolution="reserved", now=now)
                return dict(repository.task_by_id(candidate["id"]))
            return None

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

    def complete(self, task_id: int, worker_id: str, result: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["lease_owner"] != worker_id or task["status"] not in {"running", "cancel_requested"}:
                raise ConflictError("任务未由当前工作者持有")
            # 工作者在收到取消请求后才汇报完成：成果有效，按实际用量结算。
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_results WHERE task_id=?", (task_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO compute_results(task_id,version,result_json,metrics_json,result_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, version, json.dumps(result, ensure_ascii=False, sort_keys=True), json.dumps(metrics, ensure_ascii=False, sort_keys=True), digest({"result": result, "metrics": metrics}), worker_id, now),
            )
            connection.execute(
                "UPDATE compute_tasks SET status='succeeded',current_result_version=?,lease_owner='',lease_expires_at='',blocked_at='',block_reason='',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (version, now, now, task_id),
            )
            ledger = BudgetLedger(connection)
            actual_machine = self._actual_machine_seconds(task, metrics)
            ledger.settle(task_id=task_id, attempt_no=int(task["attempt_count"]), machine_seconds=actual_machine, task_units=1, now=now)
            ledger.resolve_denials(task_id=task_id, attempt_no=None, resolution="settled", now=now)
            return dict(repository.task_by_id(task_id))

    def fail(self, task_id: int, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["lease_owner"] != worker_id or task["status"] not in {"running", "cancel_requested"}:
                raise ConflictError("任务未由当前工作者持有")
            ledger = BudgetLedger(connection)
            if task["status"] == "cancel_requested":
                # 取消请求优先：工作者确认中止，任务终结，预留立即释放。
                connection.execute(
                    "UPDATE compute_tasks SET status='cancelled',available_at=?,lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (now, error_code, message[:2000], now, now, task_id),
                )
                ledger.release(task_id=task_id, attempt_no=int(task["attempt_count"]), now=now)
                ledger.resolve_denials(task_id=task_id, attempt_no=None, resolution="cancelled", now=now)
                return dict(repository.task_by_id(task_id))
            can_retry = retryable and int(task["attempt_count"]) < int(task["max_attempts"])
            status = "queued" if can_retry else "failed"
            delay = min(300, 2 ** max(0, int(task["attempt_count"]) - 1)) if can_retry else 0
            available = to_storage(now_value + timedelta(seconds=delay))
            connection.execute(
                "UPDATE compute_tasks SET status=?,available_at=?,lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (status, available, error_code, message[:2000], None if can_retry else now, now, task_id),
            )
            # 无论是否重试，本次尝试的预留都立即释放；重新排队后由下一次领取重新预留。
            ledger.release(task_id=task_id, attempt_no=int(task["attempt_count"]), now=now)
            if not can_retry:
                ledger.resolve_denials(task_id=task_id, attempt_no=None, resolution="terminal_failed", now=now)
            return dict(repository.task_by_id(task_id))

    def cancel(self, task_id: int, actor: str, reason: str, batch_key: str = "") -> dict[str, Any]:
        return self._intervene(task_id, actor, reason, "cancel", batch_key, self._cancel_mutation)

    def retry(self, task_id: int, actor: str, reason: str, priority: int | None = None, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"failed", "cancelled"}:
                raise ConflictError("只有失败或已取消任务可以人工重试")
            chosen = task["priority"] if priority is None else priority
            connection.execute("UPDATE compute_tasks SET status='queued',priority=?,available_at=?,lease_owner='',lease_expires_at='',blocked_at='',block_reason='',finished_at=NULL,updated_at=?,version=version+1 WHERE id=?", (chosen, now, now, task["id"]))
            BudgetLedger(connection).resolve_denials(task_id=task["id"], attempt_no=None, resolution="manual_retry", now=now)
        return self._intervene(task_id, actor, reason, "retry", batch_key, mutate)

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

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        recovered: list[int] = []
        exhausted: list[int] = []
        cancelled: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            ledger = BudgetLedger(connection)
            rows = connection.execute("SELECT * FROM compute_tasks WHERE status IN ('running','cancel_requested') AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id", (now,)).fetchall()
            for task in rows:
                before = dict(task)
                if task["status"] == "cancel_requested":
                    status, finished_at = "cancelled", now
                    cancelled.append(int(task["id"]))
                    resolution = "cancelled"
                elif int(task["attempt_count"]) < int(task["max_attempts"]):
                    status, finished_at = "queued", None
                    recovered.append(int(task["id"]))
                    resolution = ""
                else:
                    status, finished_at = "failed", now
                    exhausted.append(int(task["id"]))
                    resolution = "terminal_failed"
                connection.execute(
                    "UPDATE compute_tasks SET status=?,lease_owner='',lease_expires_at='',available_at=?,last_error_code=CASE WHEN ?='cancelled' THEN last_error_code ELSE 'lease_expired' END,last_error_message=CASE WHEN ?='cancelled' THEN last_error_message ELSE '工作者租约已过期' END,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (status, now, status, status, finished_at, now, task["id"]),
                )
                # 失联恢复同样要结清本次预留：释放额度，重新排队时等待下一次领取。
                ledger.release(task_id=int(task["id"]), attempt_no=int(task["attempt_count"]), now=now)
                if resolution:
                    ledger.resolve_denials(task_id=int(task["id"]), attempt_no=None, resolution=resolution, now=now)
                after = dict(repository.task_by_id(task["id"]))
                repository.add_intervention(task_id=task["id"], actor=actor, action="lease_recovery", reason="租约过期自动恢复", before=before, after=after, batch_key="", now=now)
        return {"recovered": recovered, "exhausted": exhausted, "cancelled": cancelled}

    def summary(self, period_key: str | None = None) -> dict[str, Any]:
        now_value = self.clock.now()
        ledger = BudgetLedger(self.connection)
        if period_key is None:
            period_key = ledger.resolve_period_key(now_value)
        else:
            validate_period_key(period_key)
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks GROUP BY status ORDER BY status").fetchall()
        oldest = self.connection.execute("SELECT MIN(created_at) FROM compute_tasks WHERE status='queued'").fetchone()[0]
        pools: list[dict[str, Any]] = []
        for pool in ledger.list_pools(period_key):
            totals = ledger.pool_totals(int(pool["id"]))
            denials = ledger.open_denial_totals(int(pool["id"]))
            machine_quota, task_quota = int(pool["machine_quota"]), int(pool["task_quota"])
            pools.append({
                "subject_type": pool["subject_type"],
                "subject_key": pool["subject_key"],
                "machine_seconds": {
                    "quota": machine_quota,
                    "available": machine_quota - totals["reserved_machine"] - totals["settled_machine"],
                    "reserved": totals["reserved_machine"],
                    "settled": totals["settled_machine"],
                    "released": totals["released_machine"],
                    "rejected": denials["rejected_machine"],
                },
                "tasks": {
                    "quota": task_quota,
                    "available": task_quota - totals["reserved_tasks"] - totals["settled_tasks"],
                    "reserved": totals["reserved_tasks"],
                    "settled": totals["settled_tasks"],
                    "released": totals["released_tasks"],
                    "rejected": denials["rejected_tasks"],
                },
                "rejected_count": denials["denial_count"],
            })
        return {
            "period_key": period_key,
            "fallback_period": period_key == month_period_key(now_value) and ledger.period_by_key(period_key) is None,
            "states": {row["status"]: row["amount"] for row in rows},
            "oldest_queued_at": oldest,
            "templates": len(self.repository.active_templates()),
            "budgets": pools,
        }

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

    @staticmethod
    def _cancel_mutation(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
        if task["status"] not in {"queued", "running"}:
            raise ConflictError("当前任务状态不允许取消")
        if task["status"] == "running":
            connection.execute("UPDATE compute_tasks SET status='cancel_requested',finished_at=NULL,updated_at=?,version=version+1 WHERE id=?", (now, task["id"]))
            return
        connection.execute("UPDATE compute_tasks SET status='cancelled',finished_at=?,blocked_at='',block_reason='',updated_at=?,version=version+1 WHERE id=?", (now, now, task["id"]))
        # 排队任务尚未预留额度；关闭其等待依据即可，运行中任务的预留由终结路径释放。
        BudgetLedger(connection).resolve_denials(task_id=task["id"], attempt_no=None, resolution="cancelled", now=now)

    @staticmethod
    def _actual_machine_seconds(task: sqlite3.Row, metrics: dict[str, Any]) -> int:
        raw = metrics.get("machine_seconds")
        if isinstance(raw, bool):
            raw = None
        if isinstance(raw, (int, float)) and raw >= 0:
            return max(1, int(raw))
        return max(1, int(task["estimated_machine_seconds"]))

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

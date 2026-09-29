from __future__ import annotations

from datetime import UTC, datetime

from app.compute.budget import BudgetLedger
from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.database import get_connection, transaction


TEMPLATE = {
    "code": "solver-a",
    "name": "方程求解模板",
    "algorithm": "solver-a",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {"tolerance": 0.001},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
    "estimated_machine_seconds": 100,
}

START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def submit_payload(key: str, *, user: str = "researcher-1", project: str = "course-a", priority: int = 50) -> dict:
    return {
        "template_code": "solver-a",
        "project_code": project,
        "requested_by": user,
        "parameters": {"iterations": 100, "mode": "accurate"},
        "priority": priority,
        "idempotency_key": key,
    }


def create_template(client) -> None:
    response = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert response.status_code == 201, response.text


def make_service(clock: FrozenClock | None = None) -> ComputeOperationsService:
    import os
    import tempfile

    from app.database import close_connection, init_db

    tmp_dir = tempfile.mkdtemp(prefix="compute-test-")
    os.environ["TOWNSHIP_DATABASE_PATH"] = f"{tmp_dir}/test.db"
    close_connection()
    init_db()
    return ComputeOperationsService(get_connection(), clock or FrozenClock(START))


def set_course_budget(service: ComputeOperationsService, period: str, project: str, machine: int, tasks: int, reason: str = "学期初预算") -> dict:
    return service.set_budget(
        {
            "period_key": period,
            "subject_type": "course",
            "subject_key": project,
            "machine_seconds_quota": machine,
            "task_quota": tasks,
            "reason": reason,
        },
        "administrator",
    )


def pool_summary(service: ComputeOperationsService, period: str = "month-2026-09", project: str = "course-a") -> dict:
    budgets = service.summary(period)["budgets"]
    return next(item for item in budgets if item["subject_type"] == "course" and item["subject_key"] == project)


# --------------------------------------------------------------- 模板与提交提示


def test_template_submission_idempotency_and_parameter_validation(client):
    create_template(client)
    first = client.post("/api/compute/tasks", json=submit_payload("request-000001"))
    second = client.post("/api/compute/tasks", json=submit_payload("request-000001"))
    assert first.status_code == second.status_code == 202
    body = first.json()
    assert body["id"] == second.json()["id"]
    # 提交只提示预计消耗，不产生任何预留。
    assert body["resource_preview"]["estimated_machine_seconds"] == 100
    assert body["resource_preview"]["period_key"] == "month-2026-09"
    invalid = submit_payload("request-000002")
    invalid["parameters"]["iterations"] = 20000
    rejected = client.post("/api/compute/tasks", json=invalid)
    assert rejected.status_code == 422


def test_submit_never_blocks_even_when_budget_exhausted(client):
    create_template(client)
    client.put(
        "/api/compute/budgets?actor=administrator",
        json={
            "period_key": "month-2026-09", "subject_type": "course", "subject_key": "course-a",
            "machine_seconds_quota": 0, "task_quota": 0, "reason": "预算冻结",
        },
    )
    response = client.post("/api/compute/tasks", json=submit_payload("submit-ok-001"))
    assert response.status_code == 202
    assert client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-a"], "lease_seconds": 60}).json()["task"] is None


# --------------------------------------------------------------- 领取、预留与超额


def test_claim_reserves_and_over_budget_task_keeps_waiting_basis():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=150, tasks=10)
    first = service.submit(submit_payload("budget-001", priority=90))
    second = service.submit(submit_payload("budget-002", priority=10))

    claimed = service.claim("w1", ["solver-a"], 60)
    assert claimed and claimed["id"] == first["id"]
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 100
    assert pool["machine_seconds"]["available"] == 50
    assert pool["tasks"]["reserved"] == 1

    # 第二个任务额度不足：保持排队，留下拒付依据，领取继续向后找。
    assert service.claim("w2", ["solver-a"], 60) is None
    details = service.get_task(second["id"])
    assert details["status"] == "queued"
    assert details["blocked_at"] != ""
    denial = details["resource_denials"][0]
    assert denial["required_machine_seconds"] == 100
    assert denial["available_machine_seconds"] == 50
    pool = pool_summary(service)
    assert pool["machine_seconds"]["rejected"] == 100
    assert pool["tasks"]["rejected"] == 1
    assert pool["rejected_count"] == 1

    # 管理员临时加额必须填写理由，加额后等待任务可以被领取。
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10, reason="校企临时追加机器时")
    claimed_second = service.claim("w2", ["solver-a"], 60)
    assert claimed_second and claimed_second["id"] == second["id"]
    details = service.get_task(second["id"])
    assert details["blocked_at"] == ""
    assert all(row["resolved_at"] for row in details["resource_denials"])
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 200
    assert pool["machine_seconds"]["rejected"] == 0


def test_budget_change_requires_reason_and_keeps_adjustment_trail(client):
    create_template(client)
    missing_reason = client.put(
        "/api/compute/budgets?actor=administrator",
        json={
            "period_key": "month-2026-09", "subject_type": "course", "subject_key": "course-a",
            "machine_seconds_quota": 100, "task_quota": 1,
        },
    )
    assert missing_reason.status_code == 422
    client.put(
        "/api/compute/budgets?actor=administrator",
        json={
            "period_key": "month-2026-09", "subject_type": "course", "subject_key": "course-a",
            "machine_seconds_quota": 100, "task_quota": 1, "reason": "初始额度",
        },
    )
    client.put(
        "/api/compute/budgets?actor=administrator",
        json={
            "period_key": "month-2026-09", "subject_type": "course", "subject_key": "course-a",
            "machine_seconds_quota": 250, "task_quota": 3, "reason": "竞赛周临时加额",
        },
    )
    ledger = BudgetLedger(get_connection())
    pool = ledger.pool("month-2026-09", "course", "course-a")
    adjustments = ledger.adjustments(int(pool["id"]))
    assert [(row["machine_delta"], row["task_delta"]) for row in adjustments] == [(100, 1), (150, 2)]
    assert [row["reason"] for row in adjustments] == ["初始额度", "竞赛周临时加额"]


def test_claim_skips_blocked_head_and_takes_later_candidate():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    blocked = service.submit(submit_payload("head-blocked", project="course-a", priority=90))
    affordable = service.submit(submit_payload("other-course", project="course-b", priority=10))
    set_course_budget(service, "month-2026-09", "course-a", machine=50, tasks=10)
    set_course_budget(service, "month-2026-09", "course-b", machine=500, tasks=10)
    claimed = service.claim("w1", ["solver-a"], 60)
    assert claimed and claimed["id"] == affordable["id"]
    assert service.get_task(blocked["id"])["status"] == "queued"


# --------------------------------------------------------------- 结清与释放


def test_success_settles_actual_machine_seconds():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("settle-001"))
    service.claim("w1", ["solver-a"], 60)
    service.complete(task["id"], "w1", {"value": 1}, {"machine_seconds": 80})
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["settled"] == 80
    assert pool["machine_seconds"]["available"] == 220
    assert pool["tasks"]["settled"] == 1
    details = service.get_task(task["id"])
    entry = details["resource_entries"][0]
    assert entry["state"] == "settled" and entry["final_machine_seconds"] == 80


def test_retryable_failure_and_cancel_release_or_close_reservation():
    clock = FrozenClock(START)
    service = make_service(clock)
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    retryable = service.submit(submit_payload("release-retry"))
    service.claim("w1", ["solver-a"], 60)
    failed = service.fail(retryable["id"], "w1", "numeric_error", "数值不收敛", True)
    assert failed["status"] == "queued"
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["released"] == 100
    entry = service.get_task(retryable["id"])["resource_entries"][0]
    assert entry["state"] == "released"
    # 退避结束后重新领取会再次预留。
    clock.advance(seconds=2)
    service.claim("w2", ["solver-a"], 60)
    assert pool_summary(service)["machine_seconds"]["reserved"] == 100

    queued = service.submit(submit_payload("release-cancel"))
    cancelled = service.cancel(queued["id"], "administrator", "课程停开")
    assert cancelled["status"] == "cancelled"
    details = service.get_task(queued["id"])
    assert details["interventions"][-1]["action"] == "cancel"
    assert details["resource_entries"] == []


def test_terminal_failure_releases_reservation():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("terminal-fail"))
    service.claim("w1", ["solver-a"], 60)
    result = service.fail(task["id"], "w1", "fatal", "不可恢复错误", False)
    assert result["status"] == "failed"
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["released"] == 100


def test_retry_after_failure_reserves_again():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=100, tasks=10)
    task = service.submit(submit_payload("manual-retry"))
    service.claim("w1", ["solver-a"], 60)
    service.fail(task["id"], "w1", "fatal", "不可恢复", False)
    service.retry(task["id"], "administrator", "参数修复后重跑")
    assert service.claim("w2", ["solver-a"], 60)["id"] == task["id"]
    assert pool_summary(service)["machine_seconds"]["reserved"] == 100


def test_expired_lease_recovery_releases_reservation():
    clock = FrozenClock(START)
    service = make_service(clock)
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("recovery-001"))
    service.claim("worker-a", ["solver-a"], 10)
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["recovered"] == [task["id"]]
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["released"] == 100
    # 再次领取、再次失联，超过尝试次数后失败并释放。
    assert service.claim("worker-b", ["solver-a"], 10)["id"] == task["id"]
    assert pool_summary(service)["machine_seconds"]["reserved"] == 100
    clock.advance(seconds=11)
    exhausted = service.recover_expired()
    assert exhausted["exhausted"] == [task["id"]]
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert service.get_task(task["id"])["status"] == "failed"


def test_cancel_request_on_running_task_settles_or_releases():
    clock = FrozenClock(START)
    service = make_service(clock)
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)

    finished = service.submit(submit_payload("cancel-done"))
    service.claim("w1", ["solver-a"], 600)
    requested = service.cancel(finished["id"], "administrator", "课程提前结束")
    assert requested["status"] == "cancel_requested"
    # 取消请求挂起期间任务仍在运行，预留继续保留。
    assert pool_summary(service)["machine_seconds"]["reserved"] == 100
    # 工作者仍交付了结果：按成功结算。
    service.complete(finished["id"], "w1", {"value": 1}, {"machine_seconds": 90})
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["settled"] == 90

    aborted = service.submit(submit_payload("cancel-abort"))
    service.claim("w2", ["solver-a"], 600)
    service.cancel(aborted["id"], "administrator", "课程提前结束")
    acknowledged = service.fail(aborted["id"], "w2", "aborted", "按取消请求中止", False)
    assert acknowledged["status"] == "cancelled"
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["released"] == 100


def test_cancel_request_expired_by_recovery_releases():
    clock = FrozenClock(START)
    service = make_service(clock)
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("cancel-lost"))
    service.claim("w1", ["solver-a"], 10)
    service.cancel(task["id"], "administrator", "取消后工作者失联")
    clock.advance(seconds=11)
    outcome = service.recover_expired()
    assert outcome["cancelled"] == [task["id"]]
    assert service.get_task(task["id"])["status"] == "cancelled"
    pool = pool_summary(service)
    assert pool["machine_seconds"]["reserved"] == 0
    assert pool["machine_seconds"]["released"] == 100


# --------------------------------------------------------------- 周期切换与确定性


def test_period_switch_keeps_old_occupancy_out_of_new_period():
    clock = FrozenClock(datetime(2026, 9, 30, 23, 59, 50, tzinfo=UTC))
    service = make_service(clock)
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("period-edge"))
    service.claim("w1", ["solver-a"], 600)
    september = service.summary("month-2026-09")
    pool_sep = next(item for item in september["budgets"] if item["subject_key"] == "course-a")
    assert pool_sep["machine_seconds"]["reserved"] == 100

    clock.advance(seconds=20)
    october = service.summary()
    assert october["period_key"] == "month-2026-10"
    assert october["budgets"] == []

    # 登记显式学期周期后按学期窗口归属。
    service.register_period(
        {
            "period_key": "autumn-2026",
            "starts_at": datetime(2026, 10, 1, tzinfo=UTC),
            "ends_at": datetime(2027, 1, 15, tzinfo=UTC),
        },
        "administrator",
    )
    assert service.summary()["period_key"] == "autumn-2026"
    assert service.summary("month-2026-09")["budgets"][0]["machine_seconds"]["reserved"] == 100
    details = service.get_task(task["id"])
    assert {row["period_key"] for row in details["resource_entries"]} == {"month-2026-09"}


def test_registered_period_overlapping_window_rejected():
    service = make_service()
    service.register_period(
        {
            "period_key": "spring-2026",
            "starts_at": datetime(2026, 3, 1, tzinfo=UTC),
            "ends_at": datetime(2026, 7, 1, tzinfo=UTC),
        },
        "administrator",
    )
    try:
        service.register_period(
            {
                "period_key": "spring-overlap",
                "starts_at": datetime(2026, 6, 1, tzinfo=UTC),
                "ends_at": datetime(2026, 8, 1, tzinfo=UTC),
            },
            "administrator",
        )
    except Exception as exc:
        assert exc.code == "conflict"
    else:
        raise AssertionError("重叠周期应被拒绝")


def test_summary_is_repeatable_at_fixed_time():
    service = make_service()
    service.create_template(TEMPLATE, "administrator")
    set_course_budget(service, "month-2026-09", "course-a", machine=300, tasks=10)
    task = service.submit(submit_payload("deterministic"))
    service.claim("w1", ["solver-a"], 60)
    service.complete(task["id"], "w1", {"ok": True}, {"machine_seconds": 120})
    first = service.summary("month-2026-09")
    second = service.summary("month-2026-09")
    assert first == second
    assert first["period_key"] == "month-2026-09"
    machine = first["budgets"][0]["machine_seconds"]
    assert machine == {"quota": 300, "available": 180, "reserved": 0, "settled": 120, "released": 0, "rejected": 0}


# --------------------------------------------------------------- 既有运营能力回归


def test_priority_capability_claim_and_result_version(client):
    create_template(client)
    low = client.post("/api/compute/tasks", json=submit_payload("priority-low", priority=10)).json()
    high = client.post("/api/compute/tasks", json=submit_payload("priority-high", priority=90)).json()
    no_match = client.post("/api/compute/tasks/claim", json={"worker_id": "w0", "capabilities": ["other"], "lease_seconds": 60})
    assert no_match.status_code == 200 and no_match.json()["task"] is None
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-a"], "lease_seconds": 60})
    assert claimed.status_code == 200
    assert claimed.json()["task"]["id"] == high["id"]
    completed = client.post(
        f"/api/compute/tasks/{high['id']}/complete",
        json={"worker_id": "w1", "result": {"value": 3.14}, "metrics": {"seconds": 2}},
    )
    assert completed.status_code == 200
    details = client.get(f"/api/compute/task-details/{high['id']}").json()
    assert details["status"] == "succeeded"
    assert details["current_result_version"] == 1
    assert len(details["results"]) == 1
    assert low["status"] == "queued"


def test_cancel_retry_priority_and_batch_interventions(client):
    create_template(client)
    one = client.post("/api/compute/tasks", json=submit_payload("flow-one")).json()
    cancelled = client.post(f"/api/compute/tasks/{one['id']}/cancel", json={"actor": "administrator", "reason": "项目暂停"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/compute/tasks/{one['id']}/retry", json={"actor": "administrator", "reason": "项目恢复", "priority": 95})
    assert retried.status_code == 200 and retried.json()["priority"] == 95
    other = client.post("/api/compute/tasks", json=submit_payload("batch-other", user="other-user")).json()
    batch = client.post(
        "/api/compute/tasks/batch",
        json={"task_ids": [one["id"], other["id"]], "operation": "priority", "actor": "administrator", "reason": "紧急算例", "priority": 99},
    )
    assert batch.status_code == 200
    assert len(batch.json()["succeeded"]) == 2
    details = client.get(f"/api/compute/task-details/{one['id']}").json()
    assert [item["action"] for item in details["interventions"]] == ["cancel", "retry", "priority"]

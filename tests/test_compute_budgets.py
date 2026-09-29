from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock, to_storage
from app.database import close_connection, get_connection, init_db


@pytest.fixture(autouse=True)
def isolated_database(tmp_path):
    import os

    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "budget-test.db")
    close_connection()
    yield
    close_connection()

TEMPLATE = {
    "code": "solver-b",
    "name": "预算求解模板",
    "algorithm": "solver-b",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
    },
    "default_parameters": {},
    "max_runtime_seconds": 100,
    "max_attempts": 2,
}


def _service(start: datetime | None = None) -> tuple[ComputeOperationsService, FrozenClock]:
    init_db()
    clock = FrozenClock(start or datetime(2026, 9, 1, 1, 0, tzinfo=UTC))
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    return service, clock


def _submit(service: ComputeOperationsService, key: str, *, course: str = "math-101", klass: str = "class-a", period: str | None = None, user: str = "student-1") -> dict:
    return service.submit({
        "template_code": "solver-b",
        "project_code": "project-b",
        "requested_by": user,
        "parameters": {"iterations": 10},
        "priority": 50,
        "idempotency_key": key,
        "period_key": period,
        "course_code": course,
        "class_code": klass,
    })


def _setup_budgets(service: ComputeOperationsService, period: str = "2026-autumn", *, seconds: int = 500, tasks: int = 10, class_seconds: int = 200, class_tasks: int = 5) -> None:
    service.create_period({
        "period_key": period,
        "name": "2026 秋季学期",
        "starts_at": datetime(2026, 9, 1, tzinfo=UTC),
        "ends_at": datetime(2027, 1, 31, tzinfo=UTC),
    }, "administrator")
    service.set_budget({"period_key": period, "scope_type": "course", "scope_key": "math-101", "machine_seconds_limit": seconds, "tasks_limit": tasks, "reason": "学期初核额度"}, "administrator")
    service.set_budget({"period_key": period, "scope_type": "class", "scope_key": "class-a", "machine_seconds_limit": class_seconds, "tasks_limit": class_tasks, "reason": "学期初核额度"}, "administrator")


def test_submit_only_reports_estimate_without_reserving():
    service, _ = _service()
    _setup_budgets(service)
    task = _submit(service, "budget-estimate-001")
    assert task["status"] == "queued"
    assert task["estimate"] == {"machine_seconds": 100, "tasks": 1}
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"] == {"limit": 500, "reserved": 0, "settled": 0, "rejected": 0, "available": 500}
    assert course_item["tasks"] == {"limit": 10, "reserved": 0, "settled": 0, "rejected": 0, "available": 10}


def test_claim_reserves_and_success_settles_actual_machine_seconds():
    service, clock = _service()
    _setup_budgets(service)
    task = _submit(service, "budget-settle-001")
    claimed = service.claim("worker-1", ["solver-b"], 60)
    assert claimed["reservation"] == {"machine_seconds": 100, "tasks": 1}
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"]["reserved"] == 100
    assert course_item["machine_seconds"]["available"] == 400
    assert course_item["tasks"]["reserved"] == 1
    clock.advance(seconds=30)
    service.complete(task["id"], "worker-1", {"ok": True}, {"machine_seconds": 40})
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"] == {"limit": 500, "reserved": 0, "settled": 40, "rejected": 0, "available": 460}
    assert course_item["tasks"] == {"limit": 10, "reserved": 0, "settled": 1, "rejected": 0, "available": 9}
    class_item = next(item for item in summary["items"] if item["scope_type"] == "class")
    assert class_item["machine_seconds"]["settled"] == 40
    assert class_item["tasks"]["settled"] == 1


def test_retryable_failure_releases_reservation_without_settling_task_slot():
    service, _ = _service()
    _setup_budgets(service)
    task = _submit(service, "budget-fail-retry-01")
    service.claim("worker-1", ["solver-b"], 60)
    failed = service.fail(task["id"], "worker-1", "numeric_error", "不收敛", True)
    assert failed["status"] == "queued"
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"]["reserved"] == 0
    assert course_item["machine_seconds"]["settled"] == 0
    assert course_item["tasks"]["reserved"] == 0
    assert course_item["tasks"]["available"] == 10
    details = service.get_task(task["id"])
    events = [(row["event_type"], row["scope_type"]) for row in details["budget_ledger"]]
    assert ("reserve", "course") in events and ("release", "course") in events
    assert all(row["event_type"] != "settle" or row["tasks_amount"] == 0 for row in details["budget_ledger"])


def test_terminal_failure_and_lease_recovery_settle_and_release():
    service, clock = _service()
    _setup_budgets(service, tasks=2)
    first = _submit(service, "budget-fail-final-1")
    service.claim("worker-1", ["solver-b"], 10)
    clock.advance(seconds=11)
    # 第一次失联仍可重试：机时结算、任务位释放。
    assert service.recover_expired()["recovered"] == [first["id"]]
    service.claim("worker-1", ["solver-b"], 10)
    clock.advance(seconds=11)
    # 第二次失联超过最大尝试次数：任务终结，任务位也被结算。
    recovered = service.recover_expired()
    assert recovered["exhausted"] == [first["id"]]
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"]["reserved"] == 0
    assert course_item["machine_seconds"]["settled"] == 22
    assert course_item["tasks"]["settled"] == 1


def test_recovered_retry_returns_reservation_to_pool_for_next_claim():
    service, clock = _service()
    _setup_budgets(service, seconds=150, tasks=1)
    task = _submit(service, "budget-recover-001")
    service.claim("worker-1", ["solver-b"], 10)
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["recovered"] == [task["id"]]
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"]["reserved"] == 0
    assert course_item["machine_seconds"]["settled"] == 11
    assert course_item["tasks"]["reserved"] == 0
    claimed_again = service.claim("worker-2", ["solver-b"], 60)
    assert claimed_again and claimed_again["id"] == task["id"]


def test_over_budget_submission_keeps_waiting_with_evidence():
    service, _ = _service()
    _setup_budgets(service, seconds=150, tasks=1)
    first = _submit(service, "budget-block-001")
    service.claim("worker-1", ["solver-b"], 60)
    waiting = _submit(service, "budget-block-002")
    assert waiting["status"] == "blocked_budget"
    reason = json.loads(waiting["blocked_reason_json"])
    assert reason["estimate"] == {"machine_seconds": 100, "tasks": 1}
    assert reason["shortages"]
    assert reason["period_key"] == "2026-autumn"
    blocked = service.list_blocked_tasks("2026-autumn", "course", "math-101")
    assert [item["id"] for item in blocked] == [waiting["id"]]
    # 已占用的任务先结算，释放出机时；任务位仍差一个，管理员临时加额并填写理由后放行。
    service.complete(first["id"], "worker-1", {"ok": True}, {"machine_seconds": 80})
    service.adjust_budget({"period_key": "2026-autumn", "scope_type": "course", "scope_key": "math-101", "delta_machine_seconds": 40, "delta_tasks": 1, "reason": "班级补选，增补一个任务位与机时"}, "administrator")
    approved = service.approve_blocked_task(waiting["id"], "administrator", "已有班级完成结算，额度恢复")
    assert approved["status"] == "queued"
    details = service.get_task(waiting["id"])
    assert details["interventions"][-1]["action"] == "budget_approval"


def test_cancel_waiting_task_counts_as_rejected_with_reason():
    service, _ = _service()
    _setup_budgets(service, seconds=150, tasks=1)
    first = _submit(service, "budget-reject-001")
    service.claim("worker-1", ["solver-b"], 60)
    waiting = _submit(service, "budget-reject-002")
    cancelled = service.cancel(waiting["id"], "administrator", "超额且本学期不再补额")
    assert cancelled["status"] == "cancelled"
    summary = service.budget_summary("2026-autumn")
    course_item = next(item for item in summary["items"] if item["scope_type"] == "course")
    assert course_item["machine_seconds"]["rejected"] == 100
    assert course_item["tasks"]["rejected"] == 1
    assert course_item["machine_seconds"]["available"] == 50


def test_adjust_budget_requires_reason_and_keeps_history():
    service, _ = _service()
    _setup_budgets(service, seconds=150, tasks=1)
    try:
        service.adjust_budget({"period_key": "2026-autumn", "scope_type": "course", "scope_key": "math-101", "delta_machine_seconds": 100, "delta_tasks": 0, "reason": ""}, "administrator")
    except Exception as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("缺少理由时加额必须被拒绝")
    updated = service.adjust_budget({"period_key": "2026-autumn", "scope_type": "course", "scope_key": "math-101", "delta_machine_seconds": 300, "delta_tasks": 2, "reason": "新增实训周，临时加额"}, "administrator")
    assert updated["machine_seconds_limit"] == 450
    assert updated["tasks_limit"] == 3
    history = service.list_budget_adjustments("2026-autumn", "course", "math-101")
    assert [row["reason"] for row in history] == ["学期初核额度", "新增实训周，临时加额"]
    assert history[-1]["delta_machine_seconds"] == 300


def test_period_switch_keeps_old_occupancy_out_of_new_period():
    service, clock = _service()
    _setup_budgets(service)
    old_task = _submit(service, "budget-period-001")
    lingering = _submit(service, "budget-period-001b")
    service.claim("worker-1", ["solver-b"], 60)
    service.complete(old_task["id"], "worker-1", {"ok": True}, {"machine_seconds": 60})
    service.close_period("2026-autumn", "administrator")
    service.create_period({
        "period_key": "2027-spring",
        "name": "2027 春季学期",
        "starts_at": datetime(2027, 2, 1, tzinfo=UTC),
        "ends_at": datetime(2027, 7, 31, tzinfo=UTC),
    }, "administrator")
    service.set_budget({"period_key": "2027-spring", "scope_type": "course", "scope_key": "math-101", "machine_seconds_limit": 300, "tasks_limit": 3, "reason": "新学期初核"}, "administrator")
    clock.advance(days=200)
    new_task = _submit(service, "budget-period-002", period="2027-spring")
    assert new_task["period_key"] == "2027-spring"
    spring_summary = service.budget_summary("2027-spring")
    spring_course = next(item for item in spring_summary["items"] if item["scope_type"] == "course")
    assert spring_course["machine_seconds"] == {"limit": 300, "reserved": 0, "settled": 0, "rejected": 0, "available": 300}
    autumn_summary = service.budget_summary("2026-autumn")
    autumn_course = next(item for item in autumn_summary["items"] if item["scope_type"] == "course")
    assert autumn_course["machine_seconds"]["settled"] == 60
    # 旧周期遗留的排队任务不会流入新周期：领取只拿到新周期任务，旧任务留在旧周期。
    spring_claim = service.claim("worker-2", ["solver-b"], 60)
    assert spring_claim and spring_claim["id"] == new_task["id"]
    lingering_details = service.get_task(lingering["id"])
    assert lingering_details["period_key"] == "2026-autumn" and lingering_details["status"] == "queued"
    try:
        _submit(service, "budget-period-003", period="2026-autumn")
    except Exception as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("已关闭周期不能继续提交任务")


def test_budget_summary_is_reproducible_at_fixed_time():
    service, clock = _service()
    _setup_budgets(service)
    task = _submit(service, "budget-determinism-1")
    service.claim("worker-1", ["solver-b"], 60)
    clock.advance(seconds=12)
    service.complete(task["id"], "worker-1", {"ok": True}, {"machine_seconds": 12})
    first = service.budget_summary("2026-autumn")
    second = service.budget_summary("2026-autumn")
    assert first == second
    assert first["as_of"] == to_storage(clock.now())


def test_claim_skips_tasks_when_budget_cannot_cover_estimate():
    service, _ = _service()
    _setup_budgets(service, seconds=150, tasks=10, class_seconds=90, class_tasks=10)
    task = _submit(service, "budget-class-short-1")
    # 班级额度 90 小于预计 100：提交即进入等待，领取队列里看不到它。
    assert task["status"] == "blocked_budget"
    assert service.claim("worker-1", ["solver-b"], 60) is None

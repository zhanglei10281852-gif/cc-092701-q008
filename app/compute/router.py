from __future__ import annotations

from fastapi import APIRouter, Query

from app.compute.schemas import BatchOperation, BlockedApproval, BudgetAdjust, BudgetSet, CancelRequest, PeriodCreate, PriorityRequest, QuotaSet, RetryRequest, TaskClaim, TaskFailure, TaskResult, TaskSubmit, TemplateCreate
from app.compute.service import ComputeOperationsService

router = APIRouter(prefix="/api/compute", tags=["科学计算任务运营"])


def service() -> ComputeOperationsService:
    return ComputeOperationsService()


@router.get("/templates")
def list_templates():
    return {"items": service().list_templates()}


@router.post("/templates", status_code=201)
def create_template(payload: TemplateCreate, actor: str = Query(..., min_length=1)):
    return service().create_template(payload.model_dump(), actor)


@router.put("/quotas")
def set_quota(payload: QuotaSet, actor: str = Query(..., min_length=1)):
    return service().set_quota(payload.model_dump(), actor)


@router.post("/periods", status_code=201)
def create_period(payload: PeriodCreate, actor: str = Query(..., min_length=1)):
    return service().create_period(payload.model_dump(), actor)


@router.get("/periods")
def list_periods():
    return {"items": service().list_periods()}


@router.post("/periods/{period_key}/close", status_code=200)
def close_period(period_key: str, actor: str = Query(..., min_length=1)):
    return service().close_period(period_key, actor)


@router.put("/budgets")
def set_budget(payload: BudgetSet, actor: str = Query(..., min_length=1)):
    return service().set_budget(payload.model_dump(), actor)


@router.post("/budgets/adjust")
def adjust_budget(payload: BudgetAdjust, actor: str = Query(..., min_length=1)):
    return service().adjust_budget(payload.model_dump(), actor)


@router.get("/budgets")
def list_budgets(period_key: str | None = None):
    return {"items": service().list_budgets(period_key)}


@router.get("/budgets/adjustments")
def budget_adjustments(period_key: str, scope_type: str, scope_key: str):
    return {"items": service().list_budget_adjustments(period_key, scope_type, scope_key)}


@router.get("/blocked-tasks")
def list_blocked_tasks(period_key: str | None = None, scope_type: str | None = None, scope_key: str | None = None):
    return {"items": service().list_blocked_tasks(period_key, scope_type, scope_key)}


@router.post("/tasks/{task_id}/approve-budget")
def approve_blocked_task(task_id: int, payload: BlockedApproval):
    return service().approve_blocked_task(task_id, payload.actor, payload.reason)


@router.post("/tasks", status_code=202)
def submit_task(payload: TaskSubmit):
    return service().submit(payload.model_dump())


@router.get("/tasks")
def list_tasks(status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=limit)}


@router.get("/task-details/{task_id}")
def get_task(task_id: int):
    return service().get_task(task_id)


@router.post("/tasks/claim")
def claim_task(payload: TaskClaim):
    return {"task": service().claim(payload.worker_id, payload.capabilities, payload.lease_seconds)}


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: int, payload: TaskClaim):
    return service().heartbeat(task_id, payload.worker_id, payload.lease_seconds)


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: int, payload: TaskResult):
    return service().complete(task_id, payload.worker_id, payload.result, payload.metrics)


@router.post("/tasks/{task_id}/fail")
def fail_task(task_id: int, payload: TaskFailure):
    return service().fail(task_id, payload.worker_id, payload.error_code, payload.message, payload.retryable)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, payload: CancelRequest):
    return service().cancel(task_id, payload.actor, payload.reason)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, payload: RetryRequest):
    return service().retry(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/{task_id}/priority")
def set_priority(task_id: int, payload: PriorityRequest):
    return service().set_priority(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/batch")
def batch_operation(payload: BatchOperation):
    return service().batch_operation(payload.model_dump())


@router.post("/recovery/expired-leases")
def recover_expired(actor: str = Query(default="recovery-worker", min_length=1)):
    return service().recover_expired(actor)


@router.get("/summary")
def summary():
    return service().summary()


@router.get("/budget-summary")
def budget_summary(period_key: str | None = None, scope_type: str | None = None, scope_key: str | None = None):
    return service().budget_summary(period_key, scope_type, scope_key)

from __future__ import annotations

from fastapi import APIRouter, Query

from app.compute.schemas import (
    BatchOperation,
    BudgetSet,
    CancelRequest,
    PeriodRegister,
    PriorityRequest,
    RetryRequest,
    TaskClaim,
    TaskFailure,
    TaskResult,
    TaskSubmit,
    TemplateCreate,
)
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


@router.post("/periods", status_code=201)
def register_period(payload: PeriodRegister, actor: str = Query(..., min_length=1)):
    return service().register_period(payload.model_dump(), actor)


@router.get("/periods")
def list_periods():
    return {"items": service().list_periods()}


@router.put("/budgets")
def set_budget(payload: BudgetSet, actor: str = Query(..., min_length=1)):
    return service().set_budget(payload.model_dump(), actor)


@router.get("/budgets")
def list_budgets(period_key: str | None = Query(default=None, min_length=2, max_length=64)):
    return {"items": service().list_budgets(period_key)}


@router.get("/resource-denials")
def list_denials(
    period_key: str | None = Query(default=None, min_length=2, max_length=64),
    task_id: int | None = None,
    include_resolved: bool = True,
):
    return {"items": service().list_denials(period_key=period_key, task_id=task_id, include_resolved=include_resolved)}


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
def summary(period_key: str | None = Query(default=None, min_length=2, max_length=64)):
    return service().summary(period_key)

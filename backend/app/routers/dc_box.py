"""汇流箱管理接口：维护汇流箱，覆盖排查支路、复位通讯、安排检修等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dc_box import DcBoxService

router = APIRouter(prefix="/api/dc_box", tags=["汇流箱管理"])

service = DcBoxService()

LIST_FIELDS = ["汇流箱编号", "所属组串", "输入支路", "支路电流", "箱体温度", "通讯状态", "上次检修日", "箱体状态"]
STATUSES = ["正常运行", "支路异常", "通讯中断", "检修中"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按汇流箱编号检索"),
    status: str | None = Query(default=None, description="正常运行、支路异常、通讯中断、检修中"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按汇流箱编号与状态过滤汇流箱管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出汇流箱管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dc_box", "total": total, "items": items}


@router.post("/{entry_id}/retry", response_model=ActionResult)
def retry_collection(entry_id: int) -> ActionResult:
    """对缺少输入支路或采集中断的汇流箱重新采集；恢复后按新批次重新计算。"""
    entry, message = service.retry_collection(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条汇流箱明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇流箱 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条汇流箱，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="汇流箱已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条汇流箱执行排查支路、复位通讯、安排检修；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

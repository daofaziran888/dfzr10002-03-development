"""分析数据集接口：数据集维护、数据源引用闸门与刷新动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataset import DatasetService

router = APIRouter(prefix="/api/dataset", tags=["分析数据集"])

service = DatasetService()

LIST_FIELDS = [
    "数据集编号", "数据集名称", "来源表名", "字段列表", "行数上限", "刷新频率",
    "最近刷新", "是否启用", "依赖数据源连接编号", "数据源复检结论", "数据源失败阶段",
    "数据源失败原因", "数据源最近成功探测时间", "可刷新",
]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按数据集编号或名称检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    refreshable: bool | None = Query(default=None, description="按数据源复检结论派生的可刷新标记过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按数据集编号、状态与可刷新标记过滤分析数据集列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, refreshable=refreshable, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析数据集清单：返回全量数据（含数据源复检同步快照）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataset", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据集明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据集 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记数据集；绑定的数据源复检未通过时不允许保存。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="数据集已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交校验、启用配置、停用配置、刷新数据集；复检未通过的数据源会阻断启用与刷新。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

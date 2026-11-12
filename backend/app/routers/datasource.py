"""分析数据源接口：连接维护、连通规则查询与连通性复检。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.datasource import DatasourceService

router = APIRouter(prefix="/api/datasource", tags=["分析数据源"])

service = DatasourceService()

LIST_FIELDS = [
    "连接编号", "连接名称", "数据库类型", "连接地址", "认证凭据", "默认库名",
    "连接池大小", "探测超时", "是否启用", "复检结论", "失败分级", "失败阶段",
    "失败原因", "最近成功探测时间", "最近探测时间",
]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("/rules")
def get_rules() -> dict[str, Any]:
    """连通规则：探测超时阈值、连接池范围、数据库类型标准与允许保存条件。"""
    return service.rules()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按连接编号或名称检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    probe_result: str | None = Query(default=None, description="复检结论：通过、未通过、未复检"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按连接编号、状态与复检结论过滤分析数据源列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, probe_result=probe_result, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析数据源清单：返回全量数据（含复检快照）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datasource", "total": total, "items": items}


@router.get("/{entry_id}/probe-history")
def get_probe_history(entry_id: int) -> dict[str, Any]:
    """探测历史：同一连接只保留最近一次有效复检结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据源连接 {entry_id} 不存在或已归档")
    return {
        "datasource_id": entry_id,
        "连接编号": entry.get("连接编号"),
        "items": service.list_probe_history(entry_id),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据源连接明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据源连接 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记数据源连接；不满足连通规则的连接不允许保存。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="数据源连接已登记，可发起连通性复检", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """更新连接配置；变更后历史复检结论作废，需重新复检。"""
    entry, errors = service.update_entry(entry_id, payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="数据源连接已更新，历史复检结论已作废，请重新复检", entry=entry)


@router.post("/{entry_id}/probe", response_model=ActionResult)
def probe_entry(entry_id: int) -> ActionResult:
    """连通性复检：重复提交自动合并到同一轮，仅保留一次有效结果并同步依赖数据集。"""
    entry, message, reused = service.probe(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    prefix = "复检已合并：" if reused else ""
    return ActionResult(ok=entry.get("复检结论") == "通过", message=prefix + message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据源连接执行提交校验、启用配置、停用配置。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

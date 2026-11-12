"""分析数据集业务规则：状态流转、数据源引用闸门与刷新口径都收在这里。

闸门约定：
- 新建数据集必须绑定一个已存在且最近一次复检通过的数据源，否则不允许保存；
- 启用数据集时再次校验数据源复检结论，复检未通过不允许启用；
- 刷新动作只在「可刷新」为真时放行；历史已启用数据集在数据源复检失败后
  状态不变、仍可查看，但刷新会被拒绝，直到数据源复检恢复并同轮同步回来。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.datasource import datasource_usable
from app.store import store

MODULE = "dataset"
REQUIRED_FIELDS = ["数据集编号", "数据集名称", "来源表名", "依赖数据源连接编号"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {
    "提交校验": "待校验",
    "启用配置": "已启用",
    "停用配置": "已停用",
    "刷新数据集": "已启用",
}
NEGATIVE_ACTIONS = ["停用配置"]


class DatasetService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        refreshable: bool | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("数据集编号", ""))
                or keyword in str(row.get("数据集名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if refreshable is not None:
            rows = [row for row in rows if bool(row.get("可刷新")) == refreshable]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{field}" for field in missing]

        connection_code = str(values.get("依赖数据源连接编号") or "").strip()
        usable, datasource = datasource_usable(connection_code)
        if datasource is None:
            return None, [f"数据源连接 {connection_code} 不存在，数据集无法绑定"]
        if not usable:
            reason = datasource.get("失败原因") or "最近一次连通性复检未通过"
            stage = datasource.get("失败阶段") or "未知阶段"
            return None, [
                f"数据源 {connection_code} 复检未通过（{stage}），不允许新建数据集引用：{reason}"
            ]

        with store.lock:
            rows = store.rows(MODULE)
            if any(str(row.get("数据集编号")) == str(values.get("数据集编号")) for row in rows):
                return None, [f"数据集编号 {values.get('数据集编号')} 已存在，不允许重复登记"]
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "数据集编号": values.get("数据集编号"),
                "数据集名称": values.get("数据集名称"),
                "来源表名": values.get("来源表名"),
                "字段列表": values.get("字段列表", ""),
                "行数上限": values.get("行数上限", 1000000),
                "刷新频率": values.get("刷新频率", "手动"),
                "最近刷新": None,
                "是否启用": "否",
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "依赖数据源连接编号": connection_code,
                "可刷新": True,
            }
            self._copy_datasource_snapshot(entry, datasource)
            rows.append(entry)
            store.persist()
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"数据集 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于分析数据集可执行范围"

            if action == "刷新数据集":
                return self._refresh(entry)

            if action == "启用配置":
                usable, datasource = datasource_usable(str(entry.get("依赖数据源连接编号") or ""))
                if datasource is None:
                    return None, "绑定的数据源连接不存在，不允许启用数据集"
                if not usable:
                    return None, (
                        f"数据源 {datasource.get('连接编号')} 复检未通过"
                        f"（{datasource.get('失败阶段')}），数据集不允许启用"
                    )

            target = ACTION_RULES[action]
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["是否启用"] = "是" if target == "已启用" else "否"
            store.persist()
            return dict(entry), f"数据集已{action}"

    def _refresh(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        connection_code = str(entry.get("依赖数据源连接编号") or "")
        usable, datasource = datasource_usable(connection_code)
        if not entry.get("可刷新") or not usable:
            # 历史已启用数据集仍可查看，但刷新闸门关闭，并回传最近成功探测时间作为排障线索。
            last_success = entry.get("数据源最近成功探测时间") or (
                datasource.get("最近成功探测时间") if datasource else None
            )
            detail = entry.get("数据源失败原因") or (
                datasource.get("失败原因") if datasource else "数据源不存在"
            )
            return None, (
                f"数据源 {connection_code} 当前不可用，数据集暂不可刷新：{detail}；"
                f"最近一次成功探测时间：{last_success or '无记录'}"
            )
        entry["最近刷新"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if datasource is not None:
            self._copy_datasource_snapshot(entry, datasource)
        entry["可刷新"] = True
        store.persist()
        return dict(entry), f"数据集 {entry.get('数据集编号')} 刷新成功"

    @staticmethod
    def _copy_datasource_snapshot(entry: dict[str, Any], datasource: dict[str, Any]) -> None:
        entry["数据源复检结论"] = datasource.get("复检结论")
        entry["数据源失败分级"] = datasource.get("失败分级")
        entry["数据源失败阶段"] = datasource.get("失败阶段")
        entry["数据源失败原因"] = datasource.get("失败原因")
        entry["数据源最近成功探测时间"] = datasource.get("最近成功探测时间")
        entry["数据源最近探测时间"] = datasource.get("最近探测时间")
        entry["数据源最近探测轮次"] = datasource.get("最近探测轮次")

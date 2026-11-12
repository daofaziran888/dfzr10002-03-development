"""分析数据源业务规则：连通规则校验、连通性复检与依赖清单同步都收在这里。"""
from __future__ import annotations

import threading
from typing import Any

from app.probe import new_round_id, run_probe
from app.rules import (
    POOL_SIZE_DEFAULT,
    PROBE_MODE_REAL,
    PROBE_TIMEOUT_DEFAULT,
    connectivity_rules,
    validate_connection,
)
from app.store import store

MODULE = "datasource"
DATASET_MODULE = "dataset"
HISTORY_MODULE = "probe_history"
REQUIRED_FIELDS = ["连接编号", "连接名称", "数据库类型", "连接地址", "认证凭据", "默认库名"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 同步到依赖数据集行上的探测快照字段（数据集清单直接读，不用反查数据源）。
SYNC_FIELDS = [
    ("复检结论", "数据源复检结论"),
    ("失败分级", "数据源失败分级"),
    ("失败阶段", "数据源失败阶段"),
    ("失败原因", "数据源失败原因"),
    ("最近成功探测时间", "数据源最近成功探测时间"),
    ("最近探测时间", "数据源最近探测时间"),
    ("最近探测轮次", "数据源最近探测轮次"),
]


class DatasourceService:
    """数据源连接服务。复检在途请求按连接合并，保证重复提交只产生一次有效结果。"""

    def __init__(self, probe_gate: "threading.Event | None" = None) -> None:
        # entry_id -> (轮次事件, 探测结果占位)；同连接并发复检共用同一轮。
        self._inflight: dict[int, dict[str, Any]] = {}
        self._inflight_lock = threading.Lock()
        # 探测前同步点：生产为 None（不等待）；测试可注入事件，把在途窗口稳定撑开。
        self._probe_gate = probe_gate

    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        probe_result: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("连接编号", ""))
                or keyword in str(row.get("连接名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if probe_result:
            rows = [row for row in rows if row.get("复检结论") == probe_result]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def rules(self) -> dict[str, Any]:
        return connectivity_rules.as_dict()

    def list_probe_history(self, entry_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(HISTORY_MODULE)
            if int(row.get("datasource_id", 0)) == entry_id
        ]

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{field}" for field in missing]

        entry = self._build_entry(values)
        entry.setdefault("连接池大小", POOL_SIZE_DEFAULT)
        entry.setdefault("探测超时", PROBE_TIMEOUT_DEFAULT)
        entry.setdefault("探活模式", PROBE_MODE_REAL)
        entry.setdefault("是否启用", "否")
        # 连通规则校验：一次性返回全部违规，不满足规则的连接不允许保存。
        rule_errors = validate_connection(entry)
        if rule_errors:
            return None, rule_errors

        with store.lock:
            rows = store.rows(MODULE)
            if any(str(row.get("连接编号")) == entry["连接编号"] for row in rows):
                return None, [f"连接编号 {entry['连接编号']} 已存在，不允许重复登记"]
            entry["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            self._init_probe_fields(entry)
            rows.append(entry)
            store.persist()
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, [f"数据源连接 {entry_id} 不存在或已归档"]
            # 未提交的字段沿用原值，避免部分更新把探活模式/池大小等重置为默认值。
            merged_values = {
                "连接池大小": entry.get("连接池大小"),
                "探测超时": entry.get("探测超时"),
                "探活模式": entry.get("探活模式"),
                "是否启用": entry.get("是否启用"),
            }
            merged_values.update(values)
            merged = dict(entry)
            merged.update(self._build_entry(merged_values))
            rule_errors = validate_connection(merged)
            if rule_errors:
                return None, rule_errors
            entry.update(self._build_entry(merged_values))
            # 连接参数变更后，历史探测结论作废，需要重新复检才能被数据集引用。
            self._invalidate_probe(entry)
            self._sync_dependents(entry)
            store.persist()
            return dict(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"数据源连接 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于分析数据源可执行范围"
            target = ACTION_RULES[action]
            if action == "启用配置" and entry.get("复检结论") != "通过":
                return None, "最近一次连通性复检未通过，不允许启用该数据源连接，请先复检并恢复连通性"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["是否启用"] = "是" if target == "已启用" else "否"
            store.persist()
            return dict(entry), f"数据源连接已{action}"

    # ------------------------------------------------------------------ 复检
    def probe(self, entry_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """发起连通性复检。

        返回 (连接明细, 说明, 是否复用了在途结果)。
        重复提交：同一连接已有复检在途时，调用方阻塞等待同一轮结果，只落一次有效结果。
        """
        if store.find(MODULE, entry_id) is None:
            return None, f"数据源连接 {entry_id} 不存在或已归档", False

        # 整段抢占在 inflight 锁内判定：要么成为本轮执行者，要么挂起等待同一轮。
        with self._inflight_lock:
            holder = self._inflight.get(entry_id)
            if holder is None:
                holder = {
                    "event": threading.Condition(self._inflight_lock),
                    "round_id": new_round_id(),
                    "result": None,
                }
                self._inflight[entry_id] = holder
                is_leader = True
            else:
                is_leader = False

            if not is_leader:
                # 复用在途轮次：挂起直到执行者写回结果，整个等待过程持有同一把锁。
                holder["event"].wait_for(lambda: holder["result"] is not None)
                latest = store.find(MODULE, entry_id)
                return dict(latest), f"复检正在进行中，已复用在途轮次 {holder['round_id']} 的结果", True

        try:
            if self._probe_gate is not None:
                # 仅测试注入：让执行者停在探测前，保证其他并发请求能看到在途轮次。
                self._probe_gate.wait(timeout=10)
            with store.lock:
                snapshot = dict(store.find(MODULE, entry_id))
            result = run_probe(snapshot, round_id=holder["round_id"])
            self._persist_probe_result(snapshot, result)
            latest = store.find(MODULE, entry_id)
            message = (
                f"复检通过（轮次 {result['round_id']}，耗时 {result['耗时毫秒']}ms）"
                if result["ok"]
                else f"复检未通过：{result['failed_stage']} / {result['failure_reason']}"
            )
            # 返回副本，避免调用方持有的旧引用随仓库后续变更被改写。
            return dict(latest), message, False
        finally:
            with self._inflight_lock:
                if holder["result"] is None:
                    holder["result"] = {"round_id": holder["round_id"], "ok": False}
                holder["event"].notify_all()
                self._inflight.pop(entry_id, None)

    def _persist_probe_result(self, entry: dict[str, Any], result: dict[str, Any]) -> None:
        with store.lock:
            current = store.find(MODULE, int(entry["id"]))
            if current is None:
                return
            current["最近探测时间"] = result["探测时间"]
            current["最近探测轮次"] = result["round_id"]
            current["探测耗时毫秒"] = result["耗时毫秒"]
            if result["ok"]:
                current["复检结论"] = "通过"
                current["失败分级"] = None
                current["失败阶段"] = None
                current["失败原因"] = None
                current["最近成功探测时间"] = result["探测时间"]
                current["abnormal"] = False
            else:
                current["复检结论"] = "未通过"
                current["失败分级"] = result["severity"]
                current["失败阶段"] = result["failed_stage"]
                current["失败原因"] = result["failure_reason"]
                current["abnormal"] = True

            # 探测历史：同一连接只保留最近一次有效结果，重复提交不堆积历史。
            history = store.rows(HISTORY_MODULE)
            history[:] = [row for row in history if int(row.get("datasource_id", 0)) != int(entry["id"])]
            history.append({
                "id": max((int(row.get("id", 0)) for row in history), default=0) + 1,
                "datasource_id": int(entry["id"]),
                "连接编号": current.get("连接编号"),
                "连接名称": current.get("连接名称"),
                "数据库类型": result["数据库类型"],
                "默认库名": result["默认库名"],
                "探活模式": result["探活模式"],
                "轮次": result["round_id"],
                "结论": "通过" if result["ok"] else "未通过",
                "失败分级": result["severity"],
                "失败阶段": result["failed_stage"],
                "失败原因": result["failure_reason"],
                "探测时间": result["探测时间"],
                "耗时毫秒": result["耗时毫秒"],
                "超时阈值秒": result["超时阈值秒"],
            })

            # 同一轮结果同步反映到全部依赖数据集。
            self._sync_dependents(current)
            store.persist()

    def _sync_dependents(self, datasource_entry: dict[str, Any]) -> None:
        """把数据源最新复检快照同步给引用它的全部数据集，并刷新可刷新标记。"""
        connection_code = str(datasource_entry.get("连接编号") or "")
        passed = datasource_entry.get("复检结论") == "通过"
        for dataset in store.rows(DATASET_MODULE):
            if str(dataset.get("依赖数据源连接编号") or "") != connection_code:
                continue
            for source_field, target_field in SYNC_FIELDS:
                dataset[target_field] = datasource_entry.get(source_field)
            # 历史已启用数据集保持可查看（状态不动），仅切换刷新闸门。
            dataset["可刷新"] = passed

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _build_entry(values: dict[str, Any]) -> dict[str, Any]:
        """只组装实际提交的字段；未提交项由调用方补默认值或沿用原值，避免 None 覆盖。"""
        entry = {field: values[field] for field in REQUIRED_FIELDS if field in values}
        for optional in ("连接池大小", "探测超时", "探活模式", "是否启用"):
            if optional in values and values[optional] is not None:
                entry[optional] = values[optional]
        return entry

    @staticmethod
    def _init_probe_fields(entry: dict[str, Any]) -> None:
        entry.update({
            "复检结论": "未复检",
            "失败分级": None,
            "失败阶段": None,
            "失败原因": None,
            "最近成功探测时间": None,
            "最近探测时间": None,
            "最近探测轮次": None,
            "探测耗时毫秒": None,
        })

    @staticmethod
    def _invalidate_probe(entry: dict[str, Any]) -> None:
        entry["复检结论"] = "未复检"
        entry["失败分级"] = None
        entry["失败阶段"] = None
        entry["失败原因"] = None
        entry["最近探测时间"] = None
        entry["最近探测轮次"] = None
        entry["探测耗时毫秒"] = None
        # 最近成功探测时间保留：它是历史事实，参数变更不应抹掉上次成功的时间证据。


# 供数据集服务复用的口径判断。
def datasource_usable(connection_code: str) -> tuple[bool, dict[str, Any] | None]:
    """按连接编号判断数据源是否可被数据集引用/刷新，返回 (可用, 数据源明细)。"""
    if not connection_code:
        return False, None
    for row in store.rows(MODULE):
        if str(row.get("连接编号") or "") == str(connection_code):
            return row.get("复检结论") == "通过", row
    return False, None

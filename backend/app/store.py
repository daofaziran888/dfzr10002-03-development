"""内存数据仓库 + JSON 快照持久化。

骨架默认纯内存，但连通性复检要求「最近一次成功、失败原因和探测时间持久化」，
因此在内存表之外增加一份本地 JSON 快照：任何写操作后落盘，启动时优先回放快照，
快照不存在时才灌入种子数据。真实项目里这里换成数据库访问层即可，接口不变。

通过环境变量 WORKBENCH_STORE_PATH 可指定快照位置；置为空字符串可关闭持久化
（单元测试使用，避免污染工作区）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from app.seed import SEED_ROWS

DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "workbench_store.json"


class Store:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            path = os.environ.get("WORKBENCH_STORE_PATH", str(DEFAULT_STORE_PATH))
        self._snapshot_path = Path(path) if path else None
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._snapshot_path and self._snapshot_path.exists():
            try:
                with self._snapshot_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return {name: [dict(row) for row in rows] for name, rows in data.items()}
            except (json.JSONDecodeError, OSError):
                # 快照损坏时退回种子数据，避免坏文件导致服务起不来。
                pass
        return {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}

    def persist(self) -> None:
        """把当前全量数据落盘；关闭持久化或落盘失败时不阻断业务写操作。"""
        if self._snapshot_path is None:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._snapshot_path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(self._tables, handle, ensure_ascii=False, indent=2)
            tmp_path.replace(self._snapshot_path)
        except OSError:
            pass

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
            return None

    def find_by(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """按业务字段精确查找（例如按连接编号找出全部依赖数据集）。"""
        with self._lock:
            return [row for row in self.rows(module) if str(row.get(field) or "") == str(value)]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        with self._lock:
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

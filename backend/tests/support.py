"""测试夹具：每个用例使用独立的内存数据（关闭 JSON 落盘），互不污染。

环境仅内置 FastAPI/uvicorn/pydantic，没有 httpx，无法起 TestClient，
因此用例直接驱动服务层与规则层；HTTP 行为通过路由函数签名间接覆盖。

注意：不能在 setUp 里删 app.* 模块缓存——路由模块持有的服务单例会与测试
新导入的实例分叉，导致复检在途字典等实例状态不一致。这里改为固定导入一次，
每个用例直接重置仓库表数据。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("WORKBENCH_STORE_PATH", "")

from app.seed import SEED_ROWS  # noqa: E402
from app.services.datasource import DatasourceService  # noqa: E402
from app.services.dataset import DatasetService  # noqa: E402
from app.store import store  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 深拷贝种子数据，保证用例之间与用例内部的写操作互不串数据。
        with store.lock:
            store._tables = {  # noqa: SLF001 - 测试夹具需要重置仓库
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
        self.store = store
        # 服务无状态化要求下与模块单例共用同一实例（含在途复检字典）。
        self.datasource_service = DatasourceService()
        self.dataset_service = DatasetService()

    def datasource(self, connection_code: str) -> dict:
        rows = self.store.find_by("datasource", "连接编号", connection_code)
        self.assertTrue(rows, f"种子数据缺少 {connection_code}")
        return rows[0]

    def datasets_of(self, connection_code: str) -> list[dict]:
        return self.store.find_by("dataset", "依赖数据源连接编号", connection_code)

"""端到端业务场景：复检失败 -> 依赖数据集不可刷新 -> 恢复 -> 同轮恢复。"""
from __future__ import annotations

from app.services.datasource import DatasourceService
from tests.support import StoreTestCase


class ProbeDependencyScenarioTest(StoreTestCase):
    def test_seed_has_two_datasets_sharing_dbc_0003(self) -> None:
        dependents = self.datasets_of("DBC-0003")
        codes = sorted(row["数据集编号"] for row in dependents)
        self.assertEqual(codes, ["DS-001", "DS-004"])

    def test_probe_failure_marks_both_datasets_non_refreshable_but_viewable(self) -> None:
        datasource = self.datasource("DBC-0003")
        # MySQL 密码失效：更新凭据后复检应在身份认证阶段失败。
        entry, errors = self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:expired__fail_auth__"}
        )
        self.assertEqual(errors, [])
        result, message, reused = self.datasource_service.probe(datasource["id"])
        self.assertFalse(reused)
        self.assertEqual(result["复检结论"], "未通过")
        self.assertEqual(result["失败阶段"], "身份认证")
        self.assertIn("密码", result["失败原因"])
        self.assertIn("身份认证", message)
        # 探测结果必须附数据库类型与默认库名，不能只有笼统的不可用标记。
        self.assertEqual(result["数据库类型"], "MySQL")
        self.assertEqual(result["默认库名"], "analytics_dw")

        # 两个依赖数据集在同一轮结果中变为不可刷新，并保留最近成功探测时间。
        for dataset in self.datasets_of("DBC-0003"):
            self.assertFalse(dataset["可刷新"])
            self.assertEqual(dataset["数据源复检结论"], "未通过")
            self.assertEqual(dataset["数据源失败阶段"], "身份认证")
            self.assertEqual(dataset["数据源最近探测轮次"], result["最近探测轮次"])
            self.assertEqual(dataset["数据源最近成功探测时间"], "2026-11-10 09:00:00")
            # 历史已启用数据集保持可查看：状态与启用标记不被改写。
            self.assertEqual(dataset["status"], "已启用")
            self.assertEqual(dataset["是否启用"], "是")

        # 刷新动作被拒绝，错误信息带最近成功探测时间。
        ds1 = next(row for row in self.datasets_of("DBC-0003") if row["数据集编号"] == "DS-001")
        refreshed, message = self.dataset_service.run_action(ds1["id"], "刷新数据集")
        self.assertIsNone(refreshed)
        self.assertIn("不可刷新", message)
        self.assertIn("2026-11-10 09:00:00", message)

    def test_probe_recovery_restores_both_datasets_in_same_round(self) -> None:
        datasource = self.datasource("DBC-0003")
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:expired__fail_auth__"}
        )
        failed, _, _ = self.datasource_service.probe(datasource["id"])
        self.assertEqual(failed["复检结论"], "未通过")
        self.assertTrue(
            all(not row["可刷新"] for row in self.datasets_of("DBC-0003"))
        )

        # 密码恢复：换回正常凭据后复检成功。
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:rotated_new_password"}
        )
        recovered, message, _ = self.datasource_service.probe(datasource["id"])
        self.assertEqual(recovered["复检结论"], "通过")
        self.assertIsNone(recovered["失败原因"])
        self.assertIsNotNone(recovered["最近成功探测时间"])

        # 同一轮结果把两个数据集一起恢复为可刷新。
        round_id = recovered["最近探测轮次"]
        for dataset in self.datasets_of("DBC-0003"):
            self.assertTrue(dataset["可刷新"])
            self.assertEqual(dataset["数据源复检结论"], "通过")
            self.assertEqual(dataset["数据源最近探测轮次"], round_id)

        ds4 = next(row for row in self.datasets_of("DBC-0003") if row["数据集编号"] == "DS-004")
        refreshed, message = self.dataset_service.run_action(ds4["id"], "刷新数据集")
        self.assertIsNotNone(refreshed)
        self.assertIn("刷新成功", message)
        self.assertIsNotNone(refreshed["最近刷新"])

    def test_probe_result_carries_db_type_default_db_and_stage(self) -> None:
        datasource = self.datasource("DBC-0003")
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:bad__fail_catalog__"}
        )
        result, _, _ = self.datasource_service.probe(datasource["id"])
        self.assertEqual(result["数据库类型"], "MySQL")
        self.assertEqual(result["默认库名"], "analytics_dw")
        self.assertEqual(result["失败阶段"], "默认库校验")
        self.assertEqual(result["失败分级"], "MAJOR")
        self.assertTrue(result["失败原因"])

        # 同步到依赖清单的快照也必须带类型/库名/阶段，而不是笼统的不可用标记。
        for dataset in self.datasets_of("DBC-0003"):
            self.assertEqual(dataset["数据源失败阶段"], "默认库校验")
            self.assertTrue(dataset["数据源失败原因"])


class DatasetGateTest(StoreTestCase):
    def _create_payload(self, code: str, name: str = "新数据集") -> dict:
        return {
            "数据集编号": code,
            "数据集名称": name,
            "来源表名": "dwd_demo",
            "依赖数据源连接编号": "DBC-0003",
        }

    def test_new_dataset_blocked_when_datasource_probe_failed(self) -> None:
        datasource = self.datasource("DBC-0003")
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:x__fail_auth__"}
        )
        self.datasource_service.probe(datasource["id"])

        entry, errors = self.dataset_service.create_entry(self._create_payload("DS-009"))
        self.assertIsNone(entry)
        self.assertTrue(any("复检未通过" in item for item in errors))

    def test_new_dataset_allowed_after_probe_passes(self) -> None:
        entry, errors = self.dataset_service.create_entry(self._create_payload("DS-010"))
        self.assertEqual(errors, [])
        self.assertIsNotNone(entry)
        self.assertTrue(entry["可刷新"])
        self.assertEqual(entry["数据源复检结论"], "通过")

    def test_new_dataset_rejects_unknown_datasource(self) -> None:
        payload = self._create_payload("DS-011")
        payload["依赖数据源连接编号"] = "DBC-404"
        entry, errors = self.dataset_service.create_entry(payload)
        self.assertIsNone(entry)
        self.assertTrue(any("不存在" in item for item in errors))

    def test_enable_blocked_while_datasource_unhealthy(self) -> None:
        # 先建一个草稿数据集（此时数据源健康）。
        entry, _ = self.dataset_service.create_entry(self._create_payload("DS-012"))
        datasource = self.datasource("DBC-0003")
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:x__fail_auth__"}
        )
        self.datasource_service.probe(datasource["id"])

        enabled, message = self.dataset_service.run_action(entry["id"], "启用配置")
        self.assertIsNone(enabled)
        self.assertIn("复检未通过", message)

    def test_datasource_enable_blocked_when_probe_not_passed(self) -> None:
        datasource = self.datasource("DBC-0007")
        self.assertEqual(datasource["复检结论"], "未通过")
        entry, message = self.datasource_service.run_action(datasource["id"], "启用配置")
        self.assertIsNone(entry)
        self.assertIn("复检", message)


class ProbePersistenceAndDedupTest(StoreTestCase):
    def test_history_keeps_only_latest_valid_result(self) -> None:
        datasource = self.datasource("DBC-0003")
        first, _, _ = self.datasource_service.probe(datasource["id"])
        second, _, _ = self.datasource_service.probe(datasource["id"])
        history = self.datasource_service.list_probe_history(datasource["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["轮次"], second["最近探测轮次"])
        self.assertNotEqual(first["最近探测轮次"], second["最近探测轮次"])

    def test_concurrent_probe_merged_into_single_round(self) -> None:
        import threading

        datasource = self.datasource("DBC-0005")
        # gate 把执行者拦在真实探测之前，稳定撑开「在途」窗口供其余线程抢占合并。
        gate = threading.Event()
        gated_service = DatasourceService(probe_gate=gate)
        rounds: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(5)
        ready = threading.Event()

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                ready.set()
                entry, _message, _reused = gated_service.probe(datasource["id"])
                rounds.append(entry["最近探测轮次"])
            except Exception as exc:  # pragma: no cover - 仅用于暴露并发缺陷
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        # 等所有 worker 同时进入 probe 并停在 gate 上，再放行执行者，
        # 确保 4 个跟随者能看到在途轮次并合并。
        self.assertTrue(ready.wait(timeout=5))
        threading.Event().wait(0.1)
        gate.set()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(rounds), 5)
        # 五个并发复检必须落到同一轮，只产生一次有效结果。
        self.assertEqual(len(set(rounds)), 1)
        self.assertEqual(len(gated_service.list_probe_history(datasource["id"])), 1)

    def test_sequential_probes_are_independent_rounds(self) -> None:
        # 串行（无并发重叠）的复检各自成轮，这是与并发合并相对的正常语义。
        datasource = self.datasource("DBC-0005")
        first, _, reused_first = self.datasource_service.probe(datasource["id"])
        second, _, reused_second = self.datasource_service.probe(datasource["id"])
        self.assertFalse(reused_first)
        self.assertFalse(reused_second)
        self.assertNotEqual(first["最近探测轮次"], second["最近探测轮次"])

    def test_success_then_failure_keeps_last_success_time(self) -> None:
        datasource = self.datasource("DBC-0005")
        ok, _, _ = self.datasource_service.probe(datasource["id"])
        success_time = ok["最近成功探测时间"]

        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "bi_reader:x__fail_auth__"}
        )
        failed, _, _ = self.datasource_service.probe(datasource["id"])
        self.assertEqual(failed["复检结论"], "未通过")
        self.assertEqual(failed["最近成功探测时间"], success_time)
        # 失败后产生了新的探测轮次，最近探测时间随新轮次更新（秒级时间戳可能相同，按轮次区分）。
        self.assertNotEqual(failed["最近探测轮次"], ok["最近探测轮次"])

    def test_other_datasource_dependents_not_affected(self) -> None:
        datasource = self.datasource("DBC-0003")
        self.datasource_service.update_entry(
            datasource["id"], {"认证凭据": "analyst:x__fail_auth__"}
        )
        self.datasource_service.probe(datasource["id"])

        ds2 = self.store.find_by("dataset", "数据集编号", "DS-002")[0]
        self.assertTrue(ds2["可刷新"])
        self.assertEqual(ds2["数据源复检结论"], "通过")

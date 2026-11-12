"""连通规则与复检引擎的单元测试：超时阈值、连接池范围、类型标准、保存条件与失败分级。"""
from __future__ import annotations

from app.probe import run_probe
from app.rules import (
    POOL_SIZE_MAX,
    POOL_SIZE_MIN,
    PROBE_TIMEOUT_MAX,
    PROBE_TIMEOUT_MIN,
    SEVERITY_BLOCKER,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    STAGE_AUTH,
    STAGE_CATALOG,
    STAGE_CONFIG,
    STAGE_NETWORK,
    SUPPORTED_DB_TYPES,
    validate_connection,
)
from tests.support import StoreTestCase


def valid_values(**overrides) -> dict:
    values = {
        "连接编号": "DBC-T1",
        "连接名称": "规则测试连接",
        "数据库类型": "MySQL",
        "连接地址": "127.0.0.1:3306",
        "认证凭据": "tester:pass123",
        "默认库名": "demo_db",
        "连接池大小": 10,
        "探测超时": 5,
        "探活模式": "模拟",
    }
    values.update(overrides)
    return values


class ConnectivityRuleTest(StoreTestCase):
    def test_valid_connection_passes_rules(self) -> None:
        self.assertEqual(validate_connection(valid_values()), [])

    def test_unsupported_db_type_rejected(self) -> None:
        errors = validate_connection(valid_values(数据库类型="MongoDB"))
        self.assertTrue(any("数据库类型" in item for item in errors))
        self.assertEqual(
            set(SUPPORTED_DB_TYPES),
            {"MySQL", "PostgreSQL", "Oracle", "SQLServer", "ClickHouse"},
        )

    def test_timeout_threshold_bounds(self) -> None:
        self.assertTrue(validate_connection(valid_values(探测超时=PROBE_TIMEOUT_MIN)) == [])
        self.assertTrue(validate_connection(valid_values(探测超时=PROBE_TIMEOUT_MAX)) == [])
        errors = validate_connection(valid_values(探测超时=PROBE_TIMEOUT_MAX + 1))
        self.assertTrue(any("探测超时" in item for item in errors))
        errors = validate_connection(valid_values(探测超时=0))
        self.assertTrue(any("探测超时" in item for item in errors))

    def test_pool_size_bounds(self) -> None:
        self.assertEqual(validate_connection(valid_values(连接池大小=POOL_SIZE_MIN)), [])
        self.assertEqual(validate_connection(valid_values(连接池大小=POOL_SIZE_MAX)), [])
        errors = validate_connection(valid_values(连接池大小=POOL_SIZE_MAX + 1))
        self.assertTrue(any("连接池" in item for item in errors))

    def test_address_and_db_name_format(self) -> None:
        self.assertTrue(any("连接地址" in e for e in validate_connection(valid_values(连接地址="no-port"))))
        self.assertTrue(any("端口" in e for e in validate_connection(valid_values(连接地址="h:99999"))))
        self.assertTrue(any("默认库名" in e for e in validate_connection(valid_values(默认库名=""))))
        self.assertTrue(any("默认库名" in e for e in validate_connection(valid_values(默认库名="1bad-name"))))

    def test_credential_required(self) -> None:
        errors = validate_connection(valid_values(认证凭据=""))
        self.assertTrue(any("认证凭据" in item for item in errors))

    def test_save_rejected_when_rules_fail(self) -> None:
        entry, errors = self.datasource_service.create_entry(
            valid_values(数据库类型="MongoDB", 连接编号="DBC-BAD")
        )
        self.assertIsNone(entry)
        self.assertTrue(errors)

    def test_save_accepted_and_defaults_applied(self) -> None:
        values = valid_values(连接编号="DBC-OK")
        values.pop("连接池大小")
        values.pop("探测超时")
        entry, errors = self.datasource_service.create_entry(values)
        self.assertEqual(errors, [])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["复检结论"], "未复检")
        self.assertEqual(entry["连接池大小"], 10)
        self.assertEqual(entry["探测超时"], 5)


class ProbeStageTest(StoreTestCase):
    def _entry(self, **overrides) -> dict:
        values = valid_values(**overrides)
        values["id"] = 999
        return values

    def test_probe_success_returns_type_and_db(self) -> None:
        result = run_probe(self._entry())
        self.assertTrue(result["ok"])
        self.assertEqual(result["数据库类型"], "MySQL")
        self.assertEqual(result["默认库名"], "demo_db")
        self.assertIsNone(result["failed_stage"])
        self.assertIsNotNone(result["探测时间"])

    def test_config_stage_blocker(self) -> None:
        result = run_probe(self._entry(数据库类型="MongoDB"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], STAGE_CONFIG)
        self.assertEqual(result["severity"], SEVERITY_BLOCKER)

    def test_network_stage_blocker(self) -> None:
        result = run_probe(self._entry(连接地址="10.255.255.1__fail_network__:3306"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], STAGE_NETWORK)
        self.assertEqual(result["severity"], SEVERITY_BLOCKER)

    def test_auth_stage_critical_for_invalid_password(self) -> None:
        result = run_probe(self._entry(认证凭据="tester:bad__fail_auth__"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], STAGE_AUTH)
        self.assertEqual(result["severity"], SEVERITY_CRITICAL)
        self.assertIn("密码", result["failure_reason"])

    def test_catalog_stage_major_for_missing_database(self) -> None:
        result = run_probe(self._entry(认证凭据="tester:ok__fail_catalog__"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], STAGE_CATALOG)
        self.assertEqual(result["severity"], SEVERITY_MAJOR)
        self.assertIn("demo_db", result["failure_reason"])

    def test_real_tcp_dial_to_closed_port_fails_at_network_stage(self) -> None:
        # 真实模式 + 不带模拟标记，走真实 socket 拨号；保留端口在任何环境都不会监听。
        result = run_probe(self._entry(连接地址="127.0.0.1:1", 探测超时=2, 探活模式="真实"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], STAGE_NETWORK)
        self.assertEqual(result["severity"], SEVERITY_BLOCKER)

"""分析数据源连通规则：把「什么样的连接允许保存、按什么口径探测」集中声明。

规则要点（与业务约定一一对应）：
- 数据库类型标准：仅允许受支持的五类数据库，未登记类型一律拒绝保存；
- 探测超时阈值：单次探测 1~10 秒，默认 5 秒，超出区间不允许保存；
- 连接池可用范围：连接池大小限定 1~100，默认 10；
- 连接地址：必须是 host:port 形态，端口 1~65535；
- 默认库名：必填且符合标识符规范；
- 允许保存的条件：以上规则全部通过后才允许落库，之后才能发起连通性复检。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 数据库类型标准：键为对外展示名，值为默认端口与默认探测驱动标识。
SUPPORTED_DB_TYPES: dict[str, dict[str, Any]] = {
    "MySQL": {"default_port": 3306, "driver": "mysql"},
    "PostgreSQL": {"default_port": 5432, "driver": "postgresql"},
    "Oracle": {"default_port": 1521, "driver": "oracle"},
    "SQLServer": {"default_port": 1433, "driver": "sqlserver"},
    "ClickHouse": {"default_port": 8123, "driver": "clickhouse"},
}

PROBE_TIMEOUT_MIN = 1
PROBE_TIMEOUT_MAX = 10
PROBE_TIMEOUT_DEFAULT = 5

POOL_SIZE_MIN = 1
POOL_SIZE_MAX = 100
POOL_SIZE_DEFAULT = 10

# 库名只允许字母/下划线开头，后跟字母数字下划线，长度 1~64，避免注入式脏值落库。
_DB_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HOST_PORT_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+:(\d{1,5})$")

# 失败分级：级别越高越严重；任一失败级别都阻断数据集引用，避免静默放行。
SEVERITY_BLOCKER = "BLOCKER"
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_MAJOR = "MAJOR"
SEVERITY_MINOR = "MINOR"
BLOCKING_SEVERITIES = {SEVERITY_BLOCKER, SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR}

# 探测阶段：配置预检 -> 网络建连 -> 身份认证 -> 默认库校验，顺序即排障顺序。
STAGE_CONFIG = "配置预检"
STAGE_NETWORK = "网络建连"
STAGE_AUTH = "身份认证"
STAGE_CATALOG = "默认库校验"
PROBE_STAGES = [STAGE_CONFIG, STAGE_NETWORK, STAGE_AUTH, STAGE_CATALOG]

# 探活模式：真实模式走 TCP 拨号与数据库驱动；模拟模式供无实例环境复现认证/库故障。
PROBE_MODE_REAL = "真实"
PROBE_MODE_SIMULATED = "模拟"
PROBE_MODES = [PROBE_MODE_REAL, PROBE_MODE_SIMULATED]


@dataclass(frozen=True)
class ConnectivityRules:
    """对外暴露的规则快照，前端表单与保存校验共用同一份口径。"""

    supported_db_types: list[str] = field(
        default_factory=lambda: list(SUPPORTED_DB_TYPES.keys())
    )
    probe_timeout_min: int = PROBE_TIMEOUT_MIN
    probe_timeout_max: int = PROBE_TIMEOUT_MAX
    probe_timeout_default: int = PROBE_TIMEOUT_DEFAULT
    pool_size_min: int = POOL_SIZE_MIN
    pool_size_max: int = POOL_SIZE_MAX
    pool_size_default: int = POOL_SIZE_DEFAULT
    address_format: str = "主机名或IP:端口，例如 10.0.0.8:3306"
    db_name_pattern: str = "^[A-Za-z_][A-Za-z0-9_]{0,63}$"

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported_db_types": self.supported_db_types,
            "probe_timeout": {
                "min": self.probe_timeout_min,
                "max": self.probe_timeout_max,
                "default": self.probe_timeout_default,
            },
            "connection_pool": {
                "min": self.pool_size_min,
                "max": self.pool_size_max,
                "default": self.pool_size_default,
            },
            "address_format": self.address_format,
            "db_name_pattern": self.db_name_pattern,
            "failure_severities": [
                SEVERITY_BLOCKER,
                SEVERITY_CRITICAL,
                SEVERITY_MAJOR,
                SEVERITY_MINOR,
            ],
            "probe_stages": PROBE_STAGES,
            "probe_modes": PROBE_MODES,
            "save_conditions": [
                "数据库类型必须属于受支持类型清单",
                f"探测超时阈值限定在 {PROBE_TIMEOUT_MIN}~{PROBE_TIMEOUT_MAX} 秒",
                f"连接池大小限定在 {POOL_SIZE_MIN}~{POOL_SIZE_MAX}",
                "连接地址必须为 主机:端口 且端口在 1~65535",
                "默认库名必填且符合标识符规范",
                "认证凭据（用户名/密码）不得为空",
            ],
        }


connectivity_rules = ConnectivityRules()


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate_connection(values: dict[str, Any]) -> list[str]:
    """保存前校验：返回全部违规说明；空列表代表允许保存。"""
    errors: list[str] = []

    db_type = str(values.get("数据库类型") or "").strip()
    if db_type not in SUPPORTED_DB_TYPES:
        errors.append(
            f"数据库类型「{db_type or '空'}」不在标准清单内，仅支持："
            f"{'、'.join(SUPPORTED_DB_TYPES)}"
        )

    address = str(values.get("连接地址") or "").strip()
    match = _HOST_PORT_PATTERN.match(address)
    if not match:
        errors.append("连接地址必须为 主机名或IP:端口 形态，例如 10.0.0.8:3306")
    else:
        port = int(match.group(1))
        if not 1 <= port <= 65535:
            errors.append("连接端口必须在 1~65535 之间")

    db_name = str(values.get("默认库名") or "").strip()
    if not db_name:
        errors.append("默认库名为必填项，不允许保存空库名连接")
    elif not _DB_NAME_PATTERN.match(db_name):
        errors.append("默认库名只能以字母或下划线开头，且仅含字母、数字、下划线（最长 64 位）")

    credential = str(values.get("认证凭据") or "").strip()
    if not credential:
        errors.append("认证凭据不得为空，至少需要提供用户名/密码组合")

    timeout = _coerce_int(values.get("探测超时"))
    if timeout is None:
        errors.append(f"探测超时阈值必填，合法区间为 {PROBE_TIMEOUT_MIN}~{PROBE_TIMEOUT_MAX} 秒")
    elif not PROBE_TIMEOUT_MIN <= timeout <= PROBE_TIMEOUT_MAX:
        errors.append(f"探测超时阈值 {timeout} 秒超出允许区间 {PROBE_TIMEOUT_MIN}~{PROBE_TIMEOUT_MAX} 秒")

    pool_size = _coerce_int(values.get("连接池大小"))
    if pool_size is None:
        errors.append(f"连接池大小必填，合法区间为 {POOL_SIZE_MIN}~{POOL_SIZE_MAX}")
    elif not POOL_SIZE_MIN <= pool_size <= POOL_SIZE_MAX:
        errors.append(f"连接池大小 {pool_size} 超出可用范围 {POOL_SIZE_MIN}~{POOL_SIZE_MAX}")

    probe_mode = str(values.get("探活模式") or PROBE_MODE_REAL).strip()
    if probe_mode not in PROBE_MODES:
        errors.append(f"探活模式仅支持：{'、'.join(PROBE_MODES)}")

    return errors


def parse_address(address: str) -> tuple[str, int] | None:
    match = _HOST_PORT_PATTERN.match(str(address or "").strip())
    if not match:
        return None
    return address.rsplit(":", 1)[0], int(match.group(1))

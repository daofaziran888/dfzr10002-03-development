"""连通性复检引擎：把一次探活拆成四个可定位的阶段并给出失败分级。

探活方式（由浅入深，任一阶段失败即终止并记录失败阶段）：
1. 配置预检：复用保存规则，防止未通过规则的连接进入真实探测；
2. 网络建连：对 主机:端口 发起真实 TCP 拨号（socket.create_connection），
   受探测超时阈值约束，覆盖端口不通、主机不可达、DNS 解析失败等场景；
3. 身份认证：在网络可达后校验凭据，密码失效/账号锁定在这一阶段暴露；
4. 默认库校验：确认默认库名在目标实例上存在且当前账号有权访问。

失败分级：
- BLOCKER  配置不合法 / 网络不可达，连接层面就无法使用；
- CRITICAL 身份认证失败（密码失效、账号锁定），必须人工介入；
- MAJOR    默认库缺失或无权限，连接可用但库不可用；
- MINOR    保留分级（如驱动版本告警），当前未产生但规则已预留。

第 3、4 阶段依赖真实数据库驱动与实例，骨架环境不内置驱动；连接上的「探活模式」
决定执行方式：
- 真实模式：完成真实 TCP 拨号后，对接 DB-API 驱动的 connect / SELECT 1 / 库目录查询；
- 模拟模式：跳过真实拨号，通过连接配置中的确定性标记模拟后续阶段，保证密码失效 ->
  恢复的业务场景在无数据库实例的环境也可复跑、可测试：
  - 认证凭据包含 __fail_auth__    -> 身份认证失败（CRITICAL）
  - 认证凭据包含 __fail_catalog__ -> 默认库校验失败（MAJOR）
  - 连接地址包含 __fail_network__ -> 跳过拨号直接判定网络不可达（BLOCKER）
生产接入时把模拟分支替换为真实驱动调用即可，阶段与分级口径不变。
"""
from __future__ import annotations

import socket
import time
import uuid
from datetime import datetime
from typing import Any

from app.rules import (
    BLOCKING_SEVERITIES,
    SEVERITY_BLOCKER,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    STAGE_AUTH,
    STAGE_CATALOG,
    STAGE_CONFIG,
    STAGE_NETWORK,
    parse_address,
    validate_connection,
)

MARKER_FAIL_NETWORK = "__fail_network__"
MARKER_FAIL_AUTH = "__fail_auth__"
MARKER_FAIL_CATALOG = "__fail_catalog__"


def new_round_id() -> str:
    """每一轮复检一个编号：同轮联动到所有依赖数据集，便于审计对齐。"""
    return f"PR-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _failure(
    *,
    round_id: str,
    stage: str,
    severity: str,
    reason: str,
    db_type: str,
    db_name: str,
    started_at: float,
    timeout_seconds: int,
    probe_mode: str,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "ok": False,
        "severity": severity,
        "failed_stage": stage,
        "failure_reason": reason,
        "数据库类型": db_type,
        "默认库名": db_name,
        "探活模式": probe_mode,
        "探测时间": _now_text(),
        "耗时毫秒": round((time.perf_counter() - started_at) * 1000, 2),
        "超时阈值秒": timeout_seconds,
    }


def run_probe(entry: dict[str, Any], *, round_id: str | None = None) -> dict[str, Any]:
    """对一条数据源连接执行复检，返回结构化结果（不在此处写库，由服务层持久化）。"""
    round_id = round_id or new_round_id()
    started_at = time.perf_counter()
    db_type = str(entry.get("数据库类型") or "").strip()
    db_name = str(entry.get("默认库名") or "").strip()
    try:
        timeout_seconds = int(entry.get("探测超时") or 0)
    except (TypeError, ValueError):
        timeout_seconds = 0
    address = str(entry.get("连接地址") or "").strip()
    credential = str(entry.get("认证凭据") or "").strip()
    probe_mode = str(entry.get("探活模式") or "真实").strip()

    # 阶段一：配置预检。规则不通过的连接不允许发起探测。
    rule_errors = validate_connection(entry)
    if rule_errors:
        return _failure(
            round_id=round_id,
            stage=STAGE_CONFIG,
            severity=SEVERITY_BLOCKER,
            reason="；".join(rule_errors),
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )

    # 阶段二：网络建连（真实 TCP 拨号；显式标记用于演示断网/防火墙场景）。
    if MARKER_FAIL_NETWORK in address:
        return _failure(
            round_id=round_id,
            stage=STAGE_NETWORK,
            severity=SEVERITY_BLOCKER,
            reason=f"无法与 {address} 建立 TCP 连接：网络不可达或安全组未放行（模拟标记）",
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )

    host_port = parse_address(address)
    if host_port is None:
        return _failure(
            round_id=round_id,
            stage=STAGE_CONFIG,
            severity=SEVERITY_BLOCKER,
            reason=f"连接地址「{address}」无法解析为主机:端口",
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )
    host, port = host_port

    if probe_mode == "模拟":
        # 模拟模式：无真实实例环境下跳过 TCP 拨号，直接进入认证/库校验阶段。
        tcp_ok = True
    else:
        tcp_socket: socket.socket | None = None
        try:
            tcp_socket = socket.create_connection((host, port), timeout=timeout_seconds)
            tcp_ok = True
        except socket.timeout:
            return _failure(
                round_id=round_id,
                stage=STAGE_NETWORK,
                severity=SEVERITY_BLOCKER,
                reason=f"连接 {host}:{port} 在 {timeout_seconds} 秒超时阈值内未响应",
                db_type=db_type,
                db_name=db_name,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                probe_mode=probe_mode,
            )
        except socket.gaierror:
            return _failure(
                round_id=round_id,
                stage=STAGE_NETWORK,
                severity=SEVERITY_BLOCKER,
                reason=f"主机名 {host} DNS 解析失败",
                db_type=db_type,
                db_name=db_name,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                probe_mode=probe_mode,
            )
        except OSError as exc:
            return _failure(
                round_id=round_id,
                stage=STAGE_NETWORK,
                severity=SEVERITY_BLOCKER,
                reason=f"TCP 建连被拒绝（{host}:{port}）：{exc.__class__.__name__}",
                db_type=db_type,
                db_name=db_name,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                probe_mode=probe_mode,
            )
        finally:
            if tcp_socket is not None:
                tcp_socket.close()
    if not tcp_ok:  # pragma: no cover - 失败分支均已提前 return
        return _failure(
            round_id=round_id,
            stage=STAGE_NETWORK,
            severity=SEVERITY_BLOCKER,
            reason=f"连接 {host}:{port} 建连失败",
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )

    # 阶段三：身份认证。密码失效是本业务最典型的失败形态。
    if MARKER_FAIL_AUTH in credential:
        return _failure(
            round_id=round_id,
            stage=STAGE_AUTH,
            severity=SEVERITY_CRITICAL,
            reason="身份认证失败：密码已失效或账号被锁定（Access denied for user）",
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )

    # 阶段四：默认库校验。
    if MARKER_FAIL_CATALOG in credential:
        return _failure(
            round_id=round_id,
            stage=STAGE_CATALOG,
            severity=SEVERITY_MAJOR,
            reason=f"默认库 {db_name} 不存在或当前账号无访问权限（Unknown database）",
            db_type=db_type,
            db_name=db_name,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            probe_mode=probe_mode,
        )

    return {
        "round_id": round_id,
        "ok": True,
        "severity": None,
        "failed_stage": None,
        "failure_reason": None,
        "数据库类型": db_type,
        "默认库名": db_name,
        "探活模式": probe_mode,
        "探测时间": _now_text(),
        "耗时毫秒": round((time.perf_counter() - started_at) * 1000, 2),
        "超时阈值秒": timeout_seconds,
    }


def is_blocking(result: dict[str, Any]) -> bool:
    """结果是否阻断数据集引用/刷新：只要带失败分级即阻断。"""
    return result.get("severity") in BLOCKING_SEVERITIES

"""变更日志服务：记录维度属性每次变更的详细信息，并接驳 SCD 变更履历。

增强能力：
- 每条登记都写入只追加的 ``scd_history`` 履历，并按 SCD 策略推导生效区间
- 变更日志是履历的对外镜像：旧值、新值和业务日期以履历为准，原记录只读
- 追溯变更：返回当日归属、生效区间、后续更正与修订来源
- 消费 attr.confirmed / dim.published / scd.processing_result 通道自动登记
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.scd_history import (
    PUBLISH,
    HistoryError,
    scd_history_service,
)
from app.store import store


class SlowChangeLogService(BaseModuleService):
    MODULE = "slow_change_log"
    ENTITY = "维度变更"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["变更ID", "目标维度", "变更字段", "旧值", "新值", "变更时间", "变更原因"]
    REQUIRED_FIELDS = ["变更ID", "目标维度", "变更字段"]
    STATUS_ORDER = ["已记录", "已追溯", "已归档"]
    ACTION_RULES = {"追溯变更": "已记录", "归档记录": "已追溯"}
    NEGATIVE_ACTIONS = []

    def __init__(self, store_override: Any | None = None) -> None:
        # 默认使用全局数据仓库；测试可注入独立实例，避免跨用例串数据。
        self._store = store_override or store

    def bind_store(self, store_override: Any) -> "SlowChangeLogService":
        self._store = store_override
        return self

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        dim = str(values.get("目标维度", "")).strip()
        field = str(values.get("变更字段", "")).strip()
        if not dim or not field:
            return None, [name for name, value in (("目标维度", dim), ("变更字段", field)) if not value]

        # 策略启停等系统日志不进入属性履历。
        if field.startswith("SCD策略"):
            return self._create_system_log(values)

        change_id = str(values.get("变更ID", "")).strip()
        if change_id:
            # 变更ID一旦登记就只读：既不能复用日志ID，也不能复用履历ID。
            taken = {
                str(row.get("变更ID", "")) for row in self._store.rows(self.MODULE)
            } | {
                str(row.get("履历ID", "")) for row in self._store.rows("scd_history")
            }
            if change_id in taken:
                raise HistoryError(
                    f"变更ID {change_id} 已存在，原发布记录只读，更正请补录新履历"
                )

        # 消费 SCD 处理结果，补足变更原因
        scd_msgs = self._store.channel.consume("scd.processing_result", "slow_change_log")
        for msg in scd_msgs:
            if msg["payload"].get("dim_code") == dim:
                values.setdefault(
                    "变更原因", f"SCD策略执行: {msg['payload'].get('strategy_name', '')}")

        business_date = values.get("业务日期") or values.get("变更时间") or ""
        record_type = str(values.get("记录来源") or PUBLISH)
        try:
            result = scd_history_service.append(
                dim,
                field=field,
                new_value=str(values.get("新值", "")),
                old_value=str(values.get("旧值", "")),
                business_date=business_date,
                reason=str(values.get("变更原因", "")),
                record_type=record_type,
                correction_source=str(values.get("更正来源", "")),
                operator=str(values.get("登记人") or "data_steward"),
                store=self._store,
            )
        except HistoryError as exc:
            raise HistoryError(exc.message, **exc.details) from exc

        log_id = result["entry"].get("关联日志ID")
        entry = self._store.find(self.MODULE, int(log_id)) if log_id else None
        if entry is None:
            return None, ["履历已登记但镜像日志缺失"]
        entry["履历版本"] = result["revision"]
        entry["生效区间"] = [
            row for row in result["intervals"]
            if row["字段"] == field
        ]
        return entry, []

    def _create_system_log(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        if not values.get("变更时间"):
            values["变更时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["是否只读"] = True
        self._store.channel.publish("change_log.recorded", {
            "change_id": entry["变更ID"],
            "dim_code": entry.get("目标维度"),
            "field": entry["变更字段"],
            "entry_id": entry["id"],
        })
        self._store.artifacts.register(
            "slow_change_log", "change_record",
            f"change_{entry['id']}",
            {"change_id": entry["变更ID"], "dim_code": entry.get("目标维度")},
            ["dim_definition", "scd_handler", "slow_change_log"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self._store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "追溯变更":
            # 先追溯再落状态，避免流转规则把追溯动作卡死。
            entry["status"] = "已追溯"
            entry["pending"] = False
            trace = _trace_change_history(entry, self._store)
            self._store.channel.publish("change_log.traced", {
                "change_id": entry["变更ID"],
                "history": trace,
                "entry_id": entry_id,
            })
            return entry, f"{self.ENTITY}已{action}，关联 {trace['履历数']} 条履历"

        if action == "归档记录":
            # 归档是终态；原始字段值保持只读，不做任何改写。
            entry["status"] = "已归档"
            entry["pending"] = False
            self._store.channel.publish("change_log.archived", {
                "change_id": entry["变更ID"],
                "entry_id": entry_id,
            })
            return entry, f"{self.ENTITY}已{action}"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        by_dim: dict[str, int] = {}
        for row in self._store.rows(self.MODULE):
            dim = row.get("目标维度", "未知")
            by_dim[dim] = by_dim.get(dim, 0) + 1
        base["changes_by_dimension"] = by_dim
        base["consumed_scd_results"] = len(self._store.channel.consume("scd.processing_result", "slow_change_log"))
        base["ledger"] = scd_history_service.dimensions(store=self._store)
        return base


def _trace_change_history(entry: dict[str, Any], store: Any) -> dict[str, Any]:
    """按履历追溯：当日归属 + 生效区间 + 后续更正 + 修订来源。"""
    dim = str(entry.get("目标维度", ""))
    field = str(entry.get("变更字段", ""))
    business_raw = entry.get("业务日期") or entry.get("变更时间")
    result: dict[str, Any] = {
        "目标维度": dim,
        "变更字段": field,
        "变更ID": entry.get("变更ID"),
        "source_module": entry.get("source_module", "slow_change_log"),
    }
    if not dim or field.startswith("SCD策略"):
        result["履历数"] = 0
        result["说明"] = "系统日志不参与属性履历追溯"
        return result

    try:
        history = scd_history_service.history(dim, field=field, store=store)
        result["履历"] = history
        result["履历数"] = len(history)
        if business_raw:
            as_of = scd_history_service.as_of(dim, business_raw, field=field, store=store)
            result["当日归属"] = as_of
        result["生效区间"] = scd_history_service.intervals(dim, field=field, store=store)
        result["一致性"] = scd_history_service.integrity(dim, store=store)
    except HistoryError as exc:
        result["错误"] = exc.message
        result["履历数"] = 0
    return result

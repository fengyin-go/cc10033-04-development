"""SCD 变更履历与生效区间服务。

设计目标
========
1. **履历只追加**：每条维度属性变更（首发发布 / 迟到补录 / 更正补录）都写入
   ``scd_history``，原始记录 ``是否只读=True``，任何接口都不提供修改/删除能力，
   保证“实际发过的字段值”永远可查。
2. **生效区间由履历推导**：根据维度绑定的 SCD 策略（SCD0/1/2/3/6）和每条履历的
   旧值、新值、业务日期计算生效区间，写入 ``scd_effective_interval`` 投影表。
3. **原子重算**：每次追加履历都在同一把锁内完成“校验 → 追加 → 全量重算 → 整体替换
   投影”，失败则回滚，避免并发更正后已落库区间与字段归属对不上。
4. **重叠校验**：同一维度同一属性的生效区间不得重叠；同一天只能有一个生效版本，
   同日更正按登记时间后写胜出，旧记录保留但标记为被同日更正。
5. **追溯**：按日期返回当天每个属性的取值、生效区间、来源履历、修订来源和关联更正。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from app.store import store as default_store

HISTORY_TABLE = "scd_history"
INTERVAL_TABLE = "scd_effective_interval"
META_TABLE = "scd_history_meta"

PUBLISH = "首发发布"
LATE_BACKFILL = "迟到补录"
CORRECTION = "更正补录"
CORRECTION_TYPES = {LATE_BACKFILL, CORRECTION}
RECORD_TYPES = {PUBLISH, LATE_BACKFILL, CORRECTION}

RECORD_TYPE_ALIASES = {
    "发布": PUBLISH,
    "首发": PUBLISH,
    PUBLISH: PUBLISH,
    "补录": "补录",
    "迟到": LATE_BACKFILL,
    LATE_BACKFILL: LATE_BACKFILL,
    "更正": CORRECTION,
    CORRECTION: CORRECTION,
}

DEFAULT_SCD_TYPE = "SCD类型2"
OPEN_END = "9999-12-31"
EMPTY_VALUES = {"", "N/A", "n/a", "无", "None", "null"}

# 有更正权限的维度维护人。operator 传角色 key（兼容中文别名）。
ROLES: dict[str, dict[str, Any]] = {
    "data_steward": {"label": "数据治理员", "can_publish": True, "can_correct": True},
    "dim_owner": {"label": "维度维护人", "can_publish": True, "can_correct": True},
    "publisher": {"label": "发布值班", "can_publish": True, "can_correct": False},
    "viewer": {"label": "只读访客", "can_publish": False, "can_correct": False},
}
ROLE_ALIASES = {
    "数据治理员": "data_steward",
    "维度维护人": "dim_owner",
    "发布值班": "publisher",
    "只读访客": "viewer",
}

_LOCK = threading.RLock()


class HistoryError(Exception):
    """履历操作的业务错误，路由层按 status_code 转成 HTTP 响应。"""

    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class HistoryPermissionError(HistoryError):
    status_code = 403


class HistoryConflictError(HistoryError):
    status_code = 409


class HistoryValidationError(HistoryError):
    status_code = 422


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _store(store: Any | None) -> Any:
    return store if store is not None else default_store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_business_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HistoryValidationError(f"业务日期格式不合法：{value!r}，应为 YYYY-MM-DD") from exc


def _recorded_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return _now()
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.combine(parse_business_date(text), datetime.min.time())
        except HistoryError:
            return _now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_operator(operator: str) -> tuple[str, dict[str, Any]]:
    key = ROLE_ALIASES.get(operator, operator)
    role = ROLES.get(key)
    if role is None:
        raise HistoryPermissionError(
            f"未登记的维护人角色：{operator!r}，可选：{'、'.join(ROLES)}"
        )
    return key, role


def _normalize_record_type(raw: str, late: bool) -> str:
    value = str(raw or "").strip()
    if not value:
        return LATE_BACKFILL if late else PUBLISH
    normalized = RECORD_TYPE_ALIASES.get(value, value)
    if normalized == "补录":
        return LATE_BACKFILL if late else CORRECTION
    if normalized not in RECORD_TYPES:
        raise HistoryValidationError(
            f"记录来源不合法：{raw!r}，可选：首发发布、迟到补录、更正补录"
        )
    return normalized


def _policy_for(dim: str, store: Any) -> dict[str, Any]:
    """读取维度的 SCD 策略；没有策略时读取维度定义，最后退回默认 SCD2。"""
    dim_row = next(
        (row for row in store.rows("dim_definition")
         if str(row.get("维度编码", "")) == dim),
        None,
    )
    candidates = [
        row for row in store.rows("scd_handler")
        if str(row.get("目标维度", "")) == dim
    ]
    if not candidates:
        return {
            "策略名称": f"{dim} 维度定义策略" if dim_row else "默认SCD2策略",
            "目标维度": dim,
            "SCD类型": (dim_row or {}).get("SCD类型") or DEFAULT_SCD_TYPE,
            "生效时间": "",
            "失效时间": "9999-12-31",
            "来源": "dim_definition" if dim_row else "默认",
            "策略ID": (dim_row or {}).get("id"),
            "策略状态": (dim_row or {}).get("status", ""),
        }
    enabled = [row for row in candidates if row.get("status") == "已启用"]
    pool = enabled or candidates
    row = max(pool, key=lambda item: int(item.get("id", 0)))
    return {
        "策略名称": row.get("策略名称", ""),
        "目标维度": dim,
        "SCD类型": row.get("SCD类型", DEFAULT_SCD_TYPE) or DEFAULT_SCD_TYPE,
        "生效时间": str(row.get("生效时间", "") or ""),
        "失效时间": str(row.get("失效时间", "") or ""),
        "来源": "scd_handler",
        "策略ID": row.get("id"),
        "策略状态": row.get("status", ""),
    }


def _dim_name(dim: str, store: Any) -> str:
    for row in store.rows("dim_definition"):
        if str(row.get("维度编码", "")) == dim:
            return str(row.get("维度名称", ""))
    return ""


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _end_value(end: date | None) -> str:
    return end.isoformat() if end else OPEN_END


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value)[:10]
    if text == OPEN_END or text.startswith("9999"):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _end_date(interval: dict[str, Any]) -> date | None:
    return _as_date(interval.get("生效结束"))


def _hit(interval: dict[str, Any], day: date) -> bool:
    start = date.fromisoformat(str(interval["生效开始"])[:10])
    end = _end_date(interval)
    return start <= day and (end is None or day <= end)


def _version_tag(record: dict[str, str]) -> str:
    raw = "|".join(f"{key}={record[key]}" for key in sorted(record))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _history_business_date(row: dict[str, Any]) -> date:
    return parse_business_date(row.get("业务日期") or row.get("变更时间"))


def _history_recorded_at(row: dict[str, Any]) -> datetime:
    return _recorded_at(row.get("登记时间"))


def _is_correction(row: dict[str, Any]) -> bool:
    return str(row.get("记录来源", "")) in CORRECTION_TYPES


def _resolve_history_reference(raw: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """把履历ID / 变更ID 解析到履历行。"""
    value = str(raw or "").strip()
    if not value:
        return None
    for row in rows:
        if value in {row.get("履历ID"), row.get("变更ID"), row.get("原始变更ID")}:
            return row
    return None


# ---------------------------------------------------------------------------
# 履历服务
# ---------------------------------------------------------------------------


class ScdHistoryService:
    """SCD 维度变更履历：追加、投影重算、按日追溯和一致性校验。"""

    # ------------------------------------------------------------------
    # 初始化 / 履历读取
    # ------------------------------------------------------------------

    def ensure(self, dim: str, store: Any | None = None) -> dict[str, Any]:
        """确保维度履历已建立：首次访问时从 slow_change_log 导入既有变更。"""
        store = _store(store)
        with _LOCK:
            meta = self._meta(dim, store)
            if meta is not None:
                return meta
            history = store.rows(HISTORY_TABLE)
            existing = [row for row in history if row.get("目标维度") == dim]
            if not existing:
                existing = self._bootstrap_from_log(dim, store)
            meta = {
                "id": _next_id(store.rows(META_TABLE)),
                "目标维度": dim,
                "维度名称": _dim_name(dim, store),
                "版本号": 0,
                "修复次数": 0,
                "更新时间": _iso(_now()),
                "最后事件": "初始化",
                "SCD策略": "",
                "SCD类型": "",
            }
            store.rows(META_TABLE).append(meta)
            self._recompute(dim, store, meta, reason="初始化", bump=False)
            return meta

    def history(
        self,
        dim: str,
        *,
        field: str | None = None,
        include_corrections: bool = True,
        store: Any | None = None,
    ) -> list[dict[str, Any]]:
        return self.history_page(
            dim, field=field, include_corrections=include_corrections, store=store,
        )["items"]

    def history_page(
        self,
        dim: str,
        *,
        field: str | None = None,
        include_corrections: bool = True,
        store: Any | None = None,
    ) -> dict[str, Any]:
        store = _store(store)
        meta = self.ensure(dim, store)
        rows = [row for row in store.rows(HISTORY_TABLE) if row.get("目标维度") == dim]
        if field:
            rows = [row for row in rows if row.get("变更字段") == field]
        all_field_rows = rows
        if not include_corrections:
            rows = [row for row in rows if not _is_correction(row)]
        rows.sort(key=lambda row: (row["业务日期"], _history_recorded_at(row), int(row["id"])))
        return {
            "items": [self._history_view(row, all_field_rows) for row in rows],
            "revision": int(meta.get("版本号", 0)),
            "dimension": dim,
        }

    def _history_view(self, row: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
        """履历只读视图：附加推导出的生效状态，但不写回存储。"""
        view = dict(row)
        same_day = [
            item for item in siblings
            if item.get("变更字段") == row.get("变更字段")
            and item.get("业务日期") == row.get("业务日期")
        ]
        view["生效状态"] = "生效"
        if len(same_day) > 1:
            winner = max(same_day, key=lambda item: (_history_recorded_at(item), int(item["id"])))
            if winner.get("履历ID") != row.get("履历ID"):
                view["生效状态"] = "已被同日更正"
                view["被更正为"] = winner.get("履历ID", "")
        return view

    def repair(self, dim: str, *, operator: str = "data_steward",
               store: Any | None = None) -> dict[str, Any]:
        """从履历重新推导并整体替换投影，返回修复前后的差异。"""
        store = _store(store)
        _, role = _normalize_operator(operator)
        if not role["can_correct"]:
            raise HistoryPermissionError("当前角色没有履历修复权限")
        with _LOCK:
            meta = self.ensure(dim, store)
            before = self._projection_signature(dim, store)
            result = self._recompute(dim, store, meta, reason="人工修复", bump=True)
            after = self._projection_signature(dim, store)
            changed = [
                {"字段": field, "修复前": before.get(field), "修复后": after.get(field)}
                for field in sorted(set(before) | set(after))
                if before.get(field) != after.get(field)
            ]
            result["repaired_fields"] = changed
            return result

    # ------------------------------------------------------------------
    # 追加履历
    # ------------------------------------------------------------------

    def append(
        self,
        dim: str,
        *,
        field: str,
        new_value: str,
        business_date: Any,
        old_value: str = "",
        reason: str = "",
        record_type: str = PUBLISH,
        correction_source: str = "",
        operator: str = "data_steward",
        expected_revision: int | None = None,
        store: Any | None = None,
        recorded_at: Any | None = None,
    ) -> dict[str, Any]:
        """追加一条履历并原子重算生效区间。

        - ``首发发布`` 由发布角色执行；``补录/更正`` 需要 ``can_correct`` 权限。
        - 同日首发发布会冲突；同日补录视为同日更正，后登记的一条成为当日生效版本。
        - ``expected_revision`` 用于乐观并发校验，版本不一致直接拒绝，不落任何数据。
        """
        store = _store(store)
        field = str(field or "").strip()
        new_value = str(new_value if new_value is not None else "")
        old_value = str(old_value if old_value is not None else "")
        if not dim:
            raise HistoryValidationError("目标维度不能为空")
        if not field:
            raise HistoryValidationError("变更字段不能为空")
        if not new_value.strip():
            raise HistoryValidationError("新值不能为空")
        day = parse_business_date(business_date)
        if not reason.strip():
            reason = f"{field} 变更为 {new_value}"

        operator_key, role = _normalize_operator(operator)

        with _LOCK:
            meta = self.ensure(dim, store)
            if expected_revision is not None and int(expected_revision) != int(meta["版本号"]):
                raise HistoryConflictError(
                    f"维度 {dim} 的履历版本已变化（期望 {expected_revision}，当前 {meta['版本号']}），请刷新后再补录",
                    dimension=dim,
                    expected_revision=expected_revision,
                    current_revision=meta["版本号"],
                )

            policy = _policy_for(dim, store)
            if policy["SCD类型"] == "SCD类型0":
                raise HistoryValidationError(f"{dim} 使用 SCD类型0（不可变维度），不允许追加变更履历")

            history_rows = store.rows(HISTORY_TABLE)
            existing = [row for row in history_rows if row.get("目标维度") == dim]
            # 迟到与否按“同字段”判断：别的属性发布得更晚不影响本属性的时间线。
            max_day = max(
                (_history_business_date(row) for row in existing
                 if row.get("变更字段") == field),
                default=None,
            )
            late = max_day is not None and day < max_day
            normalized_type = _normalize_record_type(record_type, late)

            if late and normalized_type == PUBLISH:
                raise HistoryValidationError(
                    f"{day.isoformat()} 早于已登记的最新业务日期 {max_day.isoformat()}，"
                    "迟到记录必须由有更正权限的维护人以补录方式登记",
                    field=field,
                    business_date=day.isoformat(),
                    latest_business_date=max_day.isoformat(),
                )
            if normalized_type == PUBLISH and not role["can_publish"]:
                raise HistoryPermissionError(f"角色 {operator_key} 没有发布权限")
            if normalized_type in CORRECTION_TYPES and not role["can_correct"]:
                raise HistoryPermissionError(
                    f"角色 {operator_key} 没有更正权限，不能补录履历"
                )

            same_day = [
                row for row in existing
                if row.get("变更字段") == field and _history_business_date(row) == day
            ]
            if same_day and normalized_type == PUBLISH:
                raise HistoryValidationError(
                    f"{field} 在 {day.isoformat()} 已有生效记录，新增变更请使用更正补录",
                    field=field,
                    business_date=day.isoformat(),
                    existing=[row["履历ID"] for row in same_day],
                )

            if normalized_type == CORRECTION and not correction_source and same_day:
                correction_source = str(same_day[-1].get("履历ID", ""))

            # 迟到补录且未显式指定更正来源时，自动挂到同字段此前最近的一条版本上。
            if normalized_type == LATE_BACKFILL and not correction_source:
                prior = [
                    row for row in existing
                    if row.get("变更字段") == field and _history_business_date(row) <= day
                ]
                prior.sort(key=lambda row: (row["业务日期"], _history_recorded_at(row), int(row["id"])))
                if prior:
                    correction_source = str(prior[-1].get("履历ID", ""))

            resolved_source = _resolve_history_reference(correction_source, existing)
            if not old_value.strip():
                # 未显式给出旧值时，按当日之前最后一个生效版本补齐，保证履历可对账。
                old_value = self._prior_value(field, day, existing) or ""

            next_id = _next_id(history_rows)
            history_id = f"HST-{next_id:06d}"
            recorded = _recorded_at(recorded_at)
            entry: dict[str, Any] = {
                "id": next_id,
                "履历ID": history_id,
                "变更ID": history_id,
                "目标维度": dim,
                "变更字段": field,
                "旧值": old_value,
                "新值": new_value,
                "业务日期": day.isoformat(),
                "变更时间": day.isoformat(),
                "登记时间": _iso(recorded),
                "记录来源": normalized_type,
                "更正来源": str(correction_source or ""),
                "更正履历ID": str(resolved_source.get("履历ID", "")) if resolved_source else "",
                "原始版本": normalized_type == PUBLISH,
                "迟到": late,
                "是否只读": True,
                "登记人": operator_key,
                "SCD策略": policy["策略名称"],
                "SCD类型": policy["SCD类型"],
                "变更原因": reason,
                "关联日志ID": None,
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            history_rows.append(entry)

            previous_revision = int(meta["版本号"])
            previous_projection = [
                dict(row) for row in store.rows(INTERVAL_TABLE)
                if row.get("目标维度") == dim
            ]
            previous_history_snapshot = [
                dict(row) for row in history_rows if row is not entry
            ]
            previous_log_length = len(store.rows("slow_change_log"))
            try:
                recompute = self._recompute(
                    dim, store, meta, reason=f"追加履历 {history_id}", bump=True
                )
                if recompute["validation"]["overlaps"]:
                    raise HistoryValidationError(
                        "补录后生效区间出现重叠，已拒绝本次写入",
                        overlaps=recompute["validation"]["overlaps"],
                    )
                log_row = self._mirror_to_change_log(entry, store)
            except Exception:
                # 失败时同时回滚履历、镜像日志与投影，避免“履历已落库、区间没落库”。
                store.rows(HISTORY_TABLE)[:] = [dict(row) for row in previous_history_snapshot]
                store.rows(INTERVAL_TABLE)[:] = [
                    row for row in store.rows(INTERVAL_TABLE)
                    if row.get("目标维度") != dim
                ] + [dict(row) for row in previous_projection]
                del store.rows("slow_change_log")[previous_log_length:]
                meta["版本号"] = previous_revision
                meta["更新时间"] = _iso(_now())
                raise

            meta["更新时间"] = _iso(_now())
            meta["最后事件"] = f"追加履历 {history_id}"
            entry["关联日志ID"] = log_row["id"]

            return {
                "ok": True,
                "entry": dict(entry),
                "revision": int(meta["版本号"]),
                "late": late,
                "policy": policy,
                "intervals": self.intervals(dim, store=store),
                "validation": recompute["validation"],
                "message": f"{entry['记录来源']} {history_id} 已登记（{day.isoformat()} 生效）",
            }

    # ------------------------------------------------------------------
    # 追溯 / 校验
    # ------------------------------------------------------------------

    def intervals(self, dim: str, *, field: str | None = None,
                  store: Any | None = None) -> list[dict[str, Any]]:
        store = _store(store)
        self.ensure(dim, store)
        rows = [
            row for row in store.rows(INTERVAL_TABLE)
            if row.get("目标维度") == dim
        ]
        if field:
            rows = [row for row in rows if row.get("字段") == field]
        rows.sort(key=lambda row: (row["字段"], row["生效开始"], int(row["id"])))
        return [dict(row) for row in rows]

    def as_of(
        self,
        dim: str,
        day: Any,
        *,
        field: str | None = None,
        store: Any | None = None,
    ) -> dict[str, Any]:
        """追溯任一天：返回当天生效的字段值、来源履历和关联更正。"""
        store = _store(store)
        self.ensure(dim, store)
        target = parse_business_date(day)
        intervals = self.intervals(dim, store=store)
        history = self.history(dim, store=store)
        if field:
            intervals = [row for row in intervals if row["字段"] == field]

        fields: list[dict[str, Any]] = []
        record: dict[str, str] = {}
        all_links: dict[str, dict[str, Any]] = {}
        for interval in intervals:
            name = interval["字段"]
            if not _hit(interval, target):
                continue
            links = self._related_corrections(interval, history, target)
            for link in links:
                all_links[link["履历ID"]] = link
            source = _resolve_history_reference(interval.get("来源履历ID", ""), history)
            future = self._next_change(history, name, target)
            fields.append({
                "字段": name,
                "值": interval["值"],
                "生效开始": interval["生效开始"],
                "生效结束": interval["生效结束"],
                "来源履历ID": interval.get("来源履历ID", ""),
                "来源记录来源": interval.get("来源记录来源", ""),
                "来源业务日期": source.get("业务日期", "") if source else "",
                "修订来源": interval.get("修订来源", ""),
                "后续更正": list(interval.get("后续更正", [])),
                "关联更正": links,
                "下一变更": future,
                "状态": "生效",
            })
            record[name] = interval["值"]

        covered_fields = {row["字段"] for row in intervals}
        missing = sorted(covered_fields - set(record))
        return {
            "目标维度": dim,
            "维度名称": _dim_name(dim, store),
            "日期": target.isoformat(),
            "记录": record,
            "字段归属": fields,
            "未覆盖字段": missing,
            "关联更正": sorted(all_links.values(), key=lambda item: item["履历ID"]),
            "版本标识": _version_tag(record),
            "说明": "按当日生效区间归集；未覆盖字段表示该日不在任何已登记区间内",
        }

    def integrity(self, dim: str, *, store: Any | None = None) -> dict[str, Any]:
        """校验持久化投影与履历推导结果是否一致，并检查只读原值。"""
        store = _store(store)
        meta = self.ensure(dim, store)
        policy = _policy_for(dim, store)
        history = self.history(dim, store=store)
        derived, report = self._derive(dim, history, policy, int(meta["版本号"]))
        persisted = self.intervals(dim, store=store)
        report["overlaps"].extend(self._detect_persisted_overlaps(persisted))
        report["ok"] = not report["overlaps"]
        drift = self._diff_intervals(derived, persisted)
        readonly = self._readonly_violations(history, store)
        overlaps = report["overlaps"]
        return {
            "目标维度": dim,
            "版本号": int(meta["版本号"]),
            "策略": policy,
            "履历数": len(history),
            "更正数": sum(1 for row in history if _is_correction(row)),
            "区间数": len(persisted),
            "ok": not overlaps and not drift and not readonly,
            "overlaps": overlaps,
            "gaps": report["gaps"],
            "continuity_breaks": report["continuity_breaks"],
            "same_day_conflicts": report["same_day_conflicts"],
            "drift": drift,
            "readonly_violations": readonly,
            "superseded": report["superseded"],
            "warnings": report["warnings"],
        }

    def dimensions(self, *, store: Any | None = None) -> list[dict[str, Any]]:
        store = _store(store)
        dims: list[str] = []
        for source in ("dim_definition", "scd_handler", HISTORY_TABLE):
            for row in store.rows(source):
                code = str(row.get("维度编码") or row.get("目标维度") or "").strip()
                if code and code not in dims:
                    dims.append(code)
        summaries = []
        for dim in dims:
            meta = self.ensure(dim, store)
            policy = _policy_for(dim, store)
            intervals = self.intervals(dim, store=store)
            history = self.history(dim, store=store)
            summaries.append({
                "维度编码": dim,
                "维度名称": _dim_name(dim, store),
                "SCD策略": policy["策略名称"],
                "SCD类型": policy["SCD类型"],
                "策略生效时间": policy["生效时间"],
                "履历数": len(history),
                "更正数": sum(1 for row in history if _is_correction(row)),
                "区间数": len(intervals),
                "版本号": int(meta["版本号"]),
                "最近业务日期": max((row["业务日期"] for row in history), default=""),
            })
        summaries.sort(key=lambda item: item["维度编码"])
        return summaries

    # ------------------------------------------------------------------
    # 内部：导入、推导、投影
    # ------------------------------------------------------------------

    def _bootstrap_from_log(self, dim: str, store: Any) -> list[dict[str, Any]]:
        log_rows = [
            row for row in store.rows("slow_change_log")
            if str(row.get("目标维度", "")) == dim
        ]
        log_rows.sort(key=lambda row: int(row.get("id", 0)))
        created: list[dict[str, Any]] = []
        policy = _policy_for(dim, store)
        for log in log_rows:
            detail = str(log.get("变更字段", "")).strip()
            if not detail or detail.startswith("SCD策略"):
                continue
            business_raw = log.get("业务日期") or log.get("变更时间")
            try:
                day = parse_business_date(business_raw)
            except HistoryError:
                continue
            history_id = str(log.get("履历ID") or f"HST-{int(log.get('id', 0)):06d}")
            used_ids = {int(row.get("id", 0)) for row in store.rows(HISTORY_TABLE)}
            row_id = int(log.get("id", 0))
            if row_id in used_ids:
                row_id = _next_id(store.rows(HISTORY_TABLE))
            record_type = str(log.get("记录来源") or PUBLISH)
            if record_type not in RECORD_TYPES:
                record_type = PUBLISH
            entry = {
                "id": row_id,
                "履历ID": history_id,
                "变更ID": str(log.get("变更ID") or history_id),
                "原始变更ID": str(log.get("变更ID") or ""),
                "目标维度": dim,
                "变更字段": detail,
                "旧值": str(log.get("旧值", "")),
                "新值": str(log.get("新值", "")),
                "业务日期": day.isoformat(),
                "变更时间": str(log.get("变更时间") or day.isoformat()),
                "登记时间": str(log.get("登记时间") or f"{day.isoformat()}T00:00:00+00:00"),
                "记录来源": record_type,
                "更正来源": str(log.get("更正来源", "") or ""),
                "更正履历ID": "",
                "原始版本": record_type == PUBLISH,
                "迟到": False,
                "是否只读": True,
                "登记人": str(log.get("登记人", "") or "seed"),
                "SCD策略": policy["策略名称"],
                "SCD类型": policy["SCD类型"],
                "变更原因": str(log.get("变更原因", "")),
                "关联日志ID": int(log.get("id", 0)),
                "status": str(log.get("status", "已记录")),
                "pending": False,
                "abnormal": False,
            }
            store.rows(HISTORY_TABLE).append(entry)
            created.append(entry)

        # 解析更正来源：允许引用履历ID或变更ID。
        by_reference: dict[str, dict[str, Any]] = {}
        for entry in created:
            by_reference[entry["履历ID"]] = entry
            if entry.get("变更ID"):
                by_reference[str(entry["变更ID"])] = entry
        for entry in created:
            source = by_reference.get(entry.get("更正来源", ""))
            if source:
                entry["更正履历ID"] = source["履历ID"]

        # 标记迟到：业务日期早于该维度已登记的最新业务日期。
        max_day: date | None = None
        for entry in sorted(created, key=lambda item: (_history_recorded_at(item), item["id"])):
            day = _history_business_date(entry)
            entry["迟到"] = max_day is not None and day < max_day
            if max_day is None or day > max_day:
                max_day = day
        return created

    def _derive(
        self,
        dim: str,
        history: list[dict[str, Any]],
        policy: dict[str, Any],
        revision: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        scd_type = policy.get("SCD类型") or DEFAULT_SCD_TYPE
        policy_start: date | None = None
        if policy.get("生效时间"):
            try:
                policy_start = parse_business_date(policy["生效时间"])
            except HistoryError:
                policy_start = None

        report: dict[str, Any] = {
            "overlaps": [],
            "gaps": [],
            "continuity_breaks": [],
            "same_day_conflicts": [],
            "superseded": [],
            "warnings": [],
            "ok": True,
        }
        rows = [row for row in history if row.get("变更字段")]
        fields = sorted({str(row["变更字段"]) for row in rows})
        intervals: list[dict[str, Any]] = []

        for field in fields:
            entries = sorted(
                [row for row in rows if row["变更字段"] == field],
                key=lambda row: (row["业务日期"], _history_recorded_at(row), int(row["id"])),
            )
            groups: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                groups.setdefault(entry["业务日期"], []).append(entry)
            winners: list[dict[str, Any]] = []
            for day_text in sorted(groups):
                group = groups[day_text]
                winner = max(group, key=lambda row: (_history_recorded_at(row), int(row["id"])))
                winners.append(winner)
                for loser in group:
                    if loser is winner:
                        continue
                    report["same_day_conflicts"].append({
                        "字段": field,
                        "日期": day_text,
                        "生效履历ID": winner["履历ID"],
                        "被覆盖履历ID": loser["履历ID"],
                        "处理": "后登记记录当日生效，原记录保留只读",
                    })
                    report["superseded"].append({
                        "字段": field,
                        "履历ID": loser["履历ID"],
                        "原因": "同日更正",
                        "生效履历ID": winner["履历ID"],
                    })

            if scd_type == "SCD类型1":
                winner = entries[-1]
                start = policy_start or _history_business_date(winner)
                intervals.append(self._build_interval(
                    dim, field, winner["新值"], start, None, winner, revision,
                    policy, value_kind="SCD类型1最新值", future_corrections=[],
                    extras={"上一值": "", "上一值来源": ""},
                ))
                for entry in entries:
                    if entry is winner:
                        continue
                    report["superseded"].append({
                        "字段": field,
                        "履历ID": entry["履历ID"],
                        "原因": "SCD类型1只保留最新值",
                        "生效履历ID": winner["履历ID"],
                    })
                continue

            if not winners:
                continue

            baseline_created = False
            first = winners[0]
            baseline_value = str(first.get("旧值", ""))
            if baseline_value.strip() and baseline_value not in EMPTY_VALUES:
                first_day = _history_business_date(first)
                baseline_start = policy_start if policy_start and policy_start <= first_day else first_day
                baseline_end = first_day - timedelta(days=1)
                if baseline_start <= baseline_end:
                    intervals.append(self._build_interval(
                        dim, field, baseline_value, baseline_start, baseline_end, first, revision,
                        policy, value_kind="初始基线", future_corrections=None,
                        extras={"上一值": "", "上一值来源": ""},
                    ))
                    baseline_created = True
                else:
                    report["warnings"].append(
                        f"{field} 首个变更 {first_day.isoformat()} 早于策略生效时间，未生成初始基线"
                    )

            for index, winner in enumerate(winners):
                start = _history_business_date(winner)
                if index + 1 < len(winners):
                    end = _history_business_date(winners[index + 1]) - timedelta(days=1)
                else:
                    end = None
                if end is not None and end < start:
                    report["warnings"].append(
                        f"{field} 在 {start.isoformat()} 的生效区间为空（同日多版本已去重）"
                    )
                    continue
                previous_value = ""
                previous_source = ""
                if index > 0:
                    previous_value = str(winners[index - 1].get("新值", ""))
                    previous_source = winners[index - 1]["履历ID"]
                elif baseline_created:
                    previous_value = baseline_value
                    previous_source = first["履历ID"]
                next_start = (
                    _history_business_date(winners[index + 1])
                    if index + 1 < len(winners) else None
                )
                corrections = self._future_corrections(
                    winner, field, entries, start, end, next_start,
                )
                if index > 0:
                    expected_old = str(winners[index - 1].get("新值", ""))
                    if str(winner.get("旧值", "")) not in EMPTY_VALUES and str(winner.get("旧值", "")) != expected_old:
                        report["continuity_breaks"].append({
                            "字段": field,
                            "业务日期": winner["业务日期"],
                            "履历ID": winner["履历ID"],
                            "登记旧值": winner.get("旧值", ""),
                            "此前生效值": expected_old,
                            "说明": "登记旧值与此前生效值不一致，按登记值保留并标记待核对",
                        })
                intervals.append(self._build_interval(
                    dim, field, winner["新值"], start, end, winner, revision, policy,
                    value_kind=(
                        "实际发布值" if winner.get("记录来源") == PUBLISH
                        else ("迟到补录值" if winner.get("记录来源") == LATE_BACKFILL else "更正补录值")
                    ),
                    future_corrections=corrections,
                    extras={"上一值": previous_value, "上一值来源": previous_source},
                ))

        # 当前标记：每个字段最后一段生效区间。
        latest_by_field: dict[str, str] = {}
        for interval in sorted(intervals, key=lambda item: item["生效开始"]):
            latest_by_field[interval["字段"]] = interval["区间ID"]
        for interval in intervals:
            interval["当前标记"] = "Y" if latest_by_field.get(interval["字段"]) == interval["区间ID"] else "N"

        # 区间重叠 / 缺口校验（同一维度同一字段）。
        for field in fields:
            ordered = sorted(
                [row for row in intervals if row["字段"] == field],
                key=lambda row: row["生效开始"],
            )
            for left, right in zip(ordered, ordered[1:]):
                left_end = _end_date(left)
                right_start = date.fromisoformat(right["生效开始"])
                if left_end is None or right_start <= left_end:
                    overlap_end = right_start if left_end is None else min(left_end, right_start)
                    report["overlaps"].append({
                        "字段": field,
                        "左区间": left["区间ID"],
                        "右区间": right["区间ID"],
                        "重叠开始": right_start.isoformat(),
                        "重叠结束": _end_value(overlap_end),
                    })
                elif right_start > left_end + timedelta(days=1):
                    report["gaps"].append({
                        "字段": field,
                        "缺口开始": (left_end + timedelta(days=1)).isoformat(),
                        "缺口结束": (right_start - timedelta(days=1)).isoformat(),
                        "左区间": left["区间ID"],
                        "右区间": right["区间ID"],
                    })

        report["ok"] = not report["overlaps"]
        return intervals, report

    def _detect_persisted_overlaps(
        self, persisted: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """对已落库投影做独立重叠检查，防止历史脏数据被重算掩盖。"""
        issues: list[dict[str, Any]] = []
        by_field: dict[str, list[dict[str, Any]]] = {}
        for row in persisted:
            by_field.setdefault(str(row.get("字段", "")), []).append(row)
        for field, rows in by_field.items():
            ordered = sorted(rows, key=lambda row: (row["生效开始"], row["生效结束"]))
            for left, right in zip(ordered, ordered[1:]):
                left_end = _end_date(left)
                right_start = date.fromisoformat(str(right["生效开始"])[:10])
                if left_end is None or right_start <= left_end:
                    issues.append({
                        "字段": field,
                        "左区间": left.get("区间ID", ""),
                        "右区间": right.get("区间ID", ""),
                        "重叠开始": right_start.isoformat(),
                        "重叠结束": _end_value(right_start if left_end is None else min(left_end, right_start)),
                    })
        return issues

    def _build_interval(
        self,
        dim: str,
        field: str,
        value: str,
        start: date,
        end: date | None,
        source: dict[str, Any],
        revision: int,
        policy: dict[str, Any],
        *,
        value_kind: str,
        future_corrections: list[str] | None,
        extras: dict[str, str],
    ) -> dict[str, Any]:
        source_id = str(source.get("履历ID", ""))
        revision_source = source_id if _is_correction(source) else ""
        corrected_id = ""
        if _is_correction(source):
            corrected_id = str(source.get("更正履历ID") or source.get("更正来源") or "")
            if corrected_id == source_id:
                corrected_id = ""
        interval_id = f"EFF-{dim}-{field}-{start.isoformat()}-{source_id}"
        return {
            "id": 0,
            "区间ID": interval_id,
            "目标维度": dim,
            "字段": field,
            "值": value,
            "生效开始": start.isoformat(),
            "生效结束": _end_value(end),
            "开放区间": end is None,
            "来源履历ID": source_id,
            "来源业务日期": source.get("业务日期", ""),
            "来源记录来源": source.get("记录来源", ""),
            "来源值状态": value_kind,
            "修订来源": revision_source,
            "被更正履历ID": corrected_id,
            "后续更正": list(future_corrections or []),
            "上一值": extras.get("上一值", ""),
            "上一值来源": extras.get("上一值来源", ""),
            "当前标记": "N",
            "SCD策略": policy.get("策略名称", ""),
            "SCD类型": policy.get("SCD类型", ""),
            "版本号": revision,
            "生效状态": "生效",
        }

    def _future_corrections(
        self,
        winner: dict[str, Any],
        field: str,
        entries: list[dict[str, Any]],
        interval_start: date,
        interval_end: date | None,
        interval_next_start: date | None,
    ) -> list[str]:
        """登记时间更晚、且会影响本区间的补录。

        包括落在区间内的补录，以及把区间截短的补录（业务日期正好是下一版本起点）。
        """
        winner_recorded = _history_recorded_at(winner)
        result = []
        for entry in entries:
            if entry is winner or not _is_correction(entry):
                continue
            entry_day = _history_business_date(entry)
            if entry_day < interval_start:
                continue
            inside = interval_end is None or entry_day <= interval_end
            truncates = interval_next_start is not None and entry_day == interval_next_start
            if not (inside or truncates):
                continue
            if _history_recorded_at(entry) <= winner_recorded:
                continue
            result.append(entry["履历ID"])
        return sorted(set(result), key=result.index)

    def _recompute(
        self,
        dim: str,
        store: Any,
        meta: dict[str, Any],
        *,
        reason: str,
        bump: bool,
    ) -> dict[str, Any]:
        policy = _policy_for(dim, store)
        history = self.history(dim, store=store)
        if bump:
            meta["版本号"] = int(meta.get("版本号", 0)) + 1
        revision = int(meta.get("版本号", 0))
        intervals, report = self._derive(dim, history, policy, revision)

        rows = store.rows(INTERVAL_TABLE)
        next_id = _next_id(rows)
        for interval in intervals:
            interval["id"] = next_id
            next_id += 1
            interval["创建时间"] = _iso(_now())
        # 整体替换：先构造完整列表再一次性换掉，避免出现半新半旧的投影。
        rows[:] = [row for row in rows if row.get("目标维度") != dim] + intervals

        meta["维度名称"] = _dim_name(dim, store)
        meta["SCD策略"] = policy.get("策略名称", "")
        meta["SCD类型"] = policy.get("SCD类型", "")
        meta["最后事件"] = reason
        return {
            "dimension": dim,
            "revision": revision,
            "policy": policy,
            "interval_count": len(intervals),
            "validation": report,
        }

    def _prior_value(
        self,
        field: str,
        day: date,
        existing: list[dict[str, Any]],
    ) -> str:
        """找到 day 当天之前生效的值，作为缺省旧值。

        先看业务日期更早的履历；若没有，再借后续最早一条履历登记的旧值
        （迟到补录落在首条变更之前时，旧值来自那条变更的“原值”）。
        """
        earlier = [
            row for row in existing
            if row.get("变更字段") == field and _history_business_date(row) < day
        ]
        if earlier:
            latest_day = max(_history_business_date(row) for row in earlier)
            same_day = [
                row for row in earlier if _history_business_date(row) == latest_day
            ]
            winner = max(same_day, key=lambda row: (_history_recorded_at(row), int(row["id"])))
            return str(winner.get("新值", ""))

        later = [
            row for row in existing
            if row.get("变更字段") == field and _history_business_date(row) > day
        ]
        if not later:
            return ""
        earliest_day = min(_history_business_date(row) for row in later)
        same_day = [
            row for row in later if _history_business_date(row) == earliest_day
        ]
        winner = max(same_day, key=lambda row: (_history_recorded_at(row), int(row["id"])))
        return str(winner.get("旧值", ""))

    def _meta(self, dim: str, store: Any) -> dict[str, Any] | None:
        for row in store.rows(META_TABLE):
            if row.get("目标维度") == dim:
                return row
        return None

    def _mirror_to_change_log(self, entry: dict[str, Any], store: Any) -> dict[str, Any]:
        log_rows = store.rows("slow_change_log")
        log_id = _next_id(log_rows)
        log_row = {
            "id": log_id,
            "变更ID": entry["履历ID"],
            "目标维度": entry["目标维度"],
            "变更字段": entry["变更字段"],
            "旧值": entry["旧值"],
            "新值": entry["新值"],
            "变更时间": entry["业务日期"],
            "变更原因": entry.get("变更原因", ""),
            "业务日期": entry["业务日期"],
            "登记时间": entry["登记时间"],
            "记录来源": entry["记录来源"],
            "更正来源": entry.get("更正来源", ""),
            "履历ID": entry["履历ID"],
            "是否只读": True,
            "迟到": entry.get("迟到", False),
            "status": "已记录",
            "pending": True,
            "abnormal": False,
        }
        log_rows.append(log_row)
        return log_row

    def _projection_signature(self, dim: str, store: Any) -> dict[str, list[tuple[Any, ...]]]:
        result: dict[str, list[tuple[Any, ...]]] = {}
        for row in store.rows(INTERVAL_TABLE):
            if row.get("目标维度") != dim:
                continue
            result.setdefault(row["字段"], []).append((
                row["生效开始"], row["生效结束"], row["值"], row.get("来源履历ID", ""),
            ))
        return {field: sorted(values) for field, values in result.items()}

    def _diff_intervals(
        self,
        derived: list[dict[str, Any]],
        persisted: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        def signature(row: dict[str, Any]) -> tuple[Any, ...]:
            return (
                row.get("字段"), row.get("生效开始"), row.get("生效结束"),
                row.get("值"), row.get("来源履历ID", ""),
            )

        derived_by = {str(row["区间ID"]): row for row in derived}
        persisted_by = {str(row["区间ID"]): row for row in persisted}
        drift = []
        for key in sorted(set(derived_by) | set(persisted_by)):
            left = derived_by.get(key)
            right = persisted_by.get(key)
            if left is None:
                drift.append({"区间ID": key, "问题": "投影多出区间", "持久化": signature(right)})
            elif right is None:
                drift.append({"区间ID": key, "问题": "投影缺少区间", "推导": signature(left)})
            elif signature(left) != signature(right):
                drift.append({
                    "区间ID": key,
                    "问题": "字段归属/生效区间不一致",
                    "推导": signature(left),
                    "持久化": signature(right),
                })
        return drift

    def _readonly_violations(
        self,
        history: list[dict[str, Any]],
        store: Any,
    ) -> list[dict[str, Any]]:
        logs = {int(row.get("id", 0)): row for row in store.rows("slow_change_log")}
        violations = []
        for entry in history:
            if not entry.get("是否只读"):
                violations.append({"履历ID": entry["履历ID"], "问题": "原始履历未标记只读"})
            log_id = entry.get("关联日志ID")
            if log_id is None:
                continue
            log = logs.get(int(log_id))
            if log is None:
                violations.append({"履历ID": entry["履历ID"], "问题": "关联变更日志不存在"})
                continue
            if str(log.get("旧值", "")) != str(entry.get("旧值", "")) or \
                    str(log.get("新值", "")) != str(entry.get("新值", "")) or \
                    str(log.get("业务日期") or log.get("变更时间", ""))[:10] != str(entry.get("业务日期", ""))[:10]:
                violations.append({
                    "履历ID": entry["履历ID"],
                    "问题": "履历与变更日志的实际发布值不一致",
                    "履历": {"旧值": entry.get("旧值"), "新值": entry.get("新值"), "业务日期": entry.get("业务日期")},
                    "日志": {"旧值": log.get("旧值"), "新值": log.get("新值"), "业务日期": log.get("业务日期") or log.get("变更时间")},
                })
        return violations

    def _related_corrections(
        self,
        interval: dict[str, Any],
        history: list[dict[str, Any]],
        day: date,
    ) -> list[dict[str, Any]]:
        field = interval["字段"]
        source_id = str(interval.get("来源履历ID", ""))
        result: dict[str, dict[str, Any]] = {}
        for entry in history:
            if entry.get("变更字段") != field or not _is_correction(entry):
                continue
            entry_day = _history_business_date(entry)
            reasons: list[str] = []
            if entry_day == day:
                reasons.append("当日更正")
            if str(entry.get("更正履历ID") or "") == source_id:
                reasons.append("区间被后续补录截断")
            if str(entry.get("履历ID")) == source_id:
                reasons.append("当日取值来源")
            if entry_day <= day and _history_recorded_at(entry) > _history_recorded_at(
                _resolve_history_reference(source_id, history) or {"登记时间": "1970-01-01T00:00:00+00:00"}
            ):
                reasons.append("追溯期内的后续补录")
            if not reasons:
                continue
            result[str(entry["履历ID"])] = {
                "履历ID": entry["履历ID"],
                "变更字段": field,
                "业务日期": entry["业务日期"],
                "旧值": entry.get("旧值", ""),
                "新值": entry.get("新值", ""),
                "记录来源": entry.get("记录来源", ""),
                "更正来源": entry.get("更正来源", ""),
                "登记时间": entry.get("登记时间", ""),
                "关联理由": reasons,
                "是否只读": True,
            }
        return sorted(result.values(), key=lambda item: (item["业务日期"], item["履历ID"]))

    def _next_change(
        self,
        history: list[dict[str, Any]],
        field: str,
        day: date,
    ) -> dict[str, Any] | None:
        candidates = [
            row for row in history
            if row.get("变更字段") == field and _history_business_date(row) > day
        ]
        if not candidates:
            return None
        row = min(candidates, key=lambda item: (_history_business_date(item), _history_recorded_at(item), int(item["id"])))
        return {
            "履历ID": row["履历ID"],
            "业务日期": row["业务日期"],
            "旧值": row.get("旧值", ""),
            "新值": row.get("新值", ""),
            "记录来源": row.get("记录来源", ""),
        }


scd_history_service = ScdHistoryService()

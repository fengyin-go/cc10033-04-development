"""SCD 变更履历与生效区间测试。

覆盖：
- 首发发布后迟到补录，重算生效区间
- 同日更正：后登记生效，原记录只读
- 更正权限与乐观并发
- 投影与履历一致、重叠检测、修复
- 追溯任一天返回变更来源与关联更正
- 变更日志镜像与只读约束
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.scd_history import (
    LATE_BACKFILL,
    PUBLISH,
    HistoryConflictError,
    HistoryPermissionError,
    HistoryValidationError,
    ScdHistoryService,
)
from app.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def service() -> ScdHistoryService:
    return ScdHistoryService()


def _intervals(service: ScdHistoryService, store: Store, dim: str, field: str):
    return [row for row in service.intervals(dim, store=store) if row["字段"] == field]


def _value_on(service: ScdHistoryService, store: Store, dim: str, field: str, day: str):
    return service.as_of(dim, day, store=store)["记录"].get(field)


class TestLateBackfill:
    """先登记周三生效变更，再补录更早的周一记录。"""

    def test_backfill_splits_interval_and_keeps_original_source(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_PRODUCT", "product_category"
        service.append(
            dim, field=field, old_value="饮料", new_value="生鲜",
            business_date="2026-09-09", record_type=PUBLISH, operator="publisher",
            reason="周三类目调整", store=store,
        )
        result = service.append(
            dim, field=field, new_value="冷冻",
            business_date="2026-09-07", record_type=LATE_BACKFILL,
            operator="dim_owner", store=store,
        )
        assert result["late"] is True
        assert result["entry"]["旧值"] == "饮料"

        spans = _intervals(service, store, dim, field)
        assert [(row["生效开始"], row["生效结束"], row["值"]) for row in spans] == [
            ("2026-01-01", "2026-08-31", "食品"),
            ("2026-09-01", "2026-09-06", "饮料"),
            ("2026-09-07", "2026-09-08", "冷冻"),
            ("2026-09-09", "9999-12-31", "生鲜"),
        ]
        # 周三发布的原始区间仍然指向原履历，只调整了结束日期。
        published = next(row for row in spans if row["值"] == "生鲜")
        assert published["来源履历ID"] == "HST-000002"
        assert published["来源记录来源"] == PUBLISH

    def test_backfill_within_baseline_keeps_history_values(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_CUSTOMER", "customer_level"
        service.append(
            dim, field=field, new_value="铂金", business_date="2026-09-10",
            record_type=LATE_BACKFILL, operator="dim_owner", store=store,
        )
        spans = _intervals(service, store, dim, field)
        assert [row["值"] for row in spans] == ["白银", "铂金", "黄金"]
        assert _value_on(service, store, dim, field, "2026-09-09") == "白银"
        assert _value_on(service, store, dim, field, "2026-09-10") == "铂金"
        assert _value_on(service, store, dim, field, "2026-09-15") == "黄金"
        # 09-15 的原始发布值仍是黄金，实际发过的字段值不被改写。
        published = next(row for row in spans if row["值"] == "黄金")
        assert published["来源记录来源"] == PUBLISH

    def test_late_publish_requires_correction_role(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_PRODUCT", "product_category"
        service.append(
            dim, field=field, new_value="生鲜", business_date="2026-09-09",
            record_type=PUBLISH, operator="publisher", store=store,
        )
        with pytest.raises(HistoryPermissionError):
            service.append(
                dim, field=field, new_value="冷冻", business_date="2026-09-07",
                record_type=LATE_BACKFILL, operator="publisher", store=store,
            )
        with pytest.raises(HistoryValidationError):
            service.append(
                dim, field=field, new_value="冷冻", business_date="2026-09-07",
                record_type=PUBLISH, operator="dim_owner", store=store,
            )


class TestSameDayCorrection:
    def test_correction_wins_and_original_is_readonly(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_SUPPLIER", "supplier_grade"
        first = service.append(
            dim, field=field, new_value="A", business_date="2026-10-07",
            record_type=PUBLISH, operator="publisher", store=store,
        )
        second = service.append(
            dim, field=field, new_value="AA", business_date="2026-10-07",
            record_type="更正补录", operator="data_steward", store=store,
        )
        assert second["entry"]["更正来源"] == first["entry"]["履历ID"]
        assert second["entry"]["旧值"] == "A"

        spans = _intervals(service, store, dim, field)
        today = next(row for row in spans if row["生效开始"] == "2026-10-07")
        assert today["值"] == "AA"
        assert today["来源履历ID"] == second["entry"]["履历ID"]
        assert today["修订来源"] == second["entry"]["履历ID"]

        history = service.history(dim, field=field, store=store)
        original = next(row for row in history if row["履历ID"] == first["entry"]["履历ID"])
        assert original["是否只读"] is True
        assert original["新值"] == "A"
        assert original["生效状态"] == "已被同日更正"
        assert original["被更正为"] == second["entry"]["履历ID"]

        # 同日两个版本不会让同一天归属两个生效版本。
        assert len([row for row in spans if row["值"] == "AA"]) == 1
        report = service.integrity(dim, store=store)
        assert report["overlaps"] == []
        assert report["same_day_conflicts"]

    def test_same_day_second_publish_rejected(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_SUPPLIER", "supplier_grade"
        service.append(
            dim, field=field, new_value="A", business_date="2026-10-07",
            record_type=PUBLISH, operator="publisher", store=store,
        )
        with pytest.raises(HistoryValidationError):
            service.append(
                dim, field=field, new_value="B", business_date="2026-10-07",
                record_type=PUBLISH, operator="publisher", store=store,
            )


class TestPermissionsAndConcurrency:
    def test_viewer_cannot_publish(self, store: Store, service: ScdHistoryService) -> None:
        with pytest.raises(HistoryPermissionError):
            service.append(
                "DIM_PRODUCT", field="product_name", new_value="新名称",
                business_date="2026-10-01", record_type=PUBLISH,
                operator="viewer", store=store,
            )

    def test_stale_revision_is_rejected(self, store: Store, service: ScdHistoryService) -> None:
        dim = "DIM_PRODUCT"
        service.append(dim, field="product_name", new_value="A", business_date="2026-10-01",
                       record_type=PUBLISH, operator="publisher", store=store)
        with pytest.raises(HistoryConflictError):
            service.append(dim, field="product_name", new_value="B", business_date="2026-10-02",
                           record_type=PUBLISH, operator="publisher",
                           expected_revision=0, store=store)

    def test_concurrent_corrections_only_one_wins(self, store: Store) -> None:
        service = ScdHistoryService()
        dim, field = "DIM_PRODUCT", "product_name"
        first = service.append(
            dim, field=field, new_value="A", business_date="2026-10-01",
            record_type=PUBLISH, operator="publisher", store=store,
        )
        base = first["revision"]

        def attempt(value: str):
            try:
                return service.append(
                    dim, field=field, new_value=value, business_date="2026-10-01",
                    record_type="更正补录", operator="data_steward",
                    expected_revision=base, store=store,
                )["revision"]
            except HistoryConflictError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, ["A1", "A2", "A3", "A4"]))
        assert sum(1 for item in results if item is not None) == 1
        spans = _intervals(service, store, dim, field)
        assert len([row for row in spans if row["生效开始"] == "2026-10-01"]) == 1
        assert service.integrity(dim, store=store)["overlaps"] == []


class TestTraceability:
    def test_as_of_returns_source_and_correction_links(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_PRODUCT", "product_category"
        service.append(dim, field=field, old_value="饮料", new_value="生鲜",
                       business_date="2026-09-09", record_type=PUBLISH,
                       operator="publisher", store=store)
        backfill = service.append(dim, field=field, new_value="冷冻",
                                  business_date="2026-09-07", record_type=LATE_BACKFILL,
                                  operator="dim_owner", store=store)

        before = service.as_of(dim, "2026-09-06", store=store)
        field_row = next(row for row in before["字段归属"] if row["字段"] == field)
        assert field_row["值"] == "饮料"
        assert field_row["来源履历ID"] == "HST-000001"
        assert field_row["下一变更"]["履历ID"] == backfill["entry"]["履历ID"]

        during = service.as_of(dim, "2026-09-08", store=store)
        field_row = next(row for row in during["字段归属"] if row["字段"] == field)
        assert field_row["值"] == "冷冻"
        assert field_row["来源记录来源"] == LATE_BACKFILL
        assert [link["履历ID"] for link in during["关联更正"]] == [backfill["entry"]["履历ID"]]
        assert "当日取值来源" in during["关联更正"][0]["关联理由"]

    def test_day_without_intervals_is_explicit(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        result = service.as_of("DIM_CUSTOMER", "2025-12-31", store=store)
        assert result["记录"] == {}
        assert result["未覆盖字段"] == ["customer_level"]


class TestIntegrityAndRepair:
    def test_projection_drift_detected_and_repaired(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        dim, field = "DIM_PRODUCT", "product_category"
        service.append(dim, field=field, new_value="生鲜", business_date="2026-09-09",
                       record_type=PUBLISH, operator="publisher", store=store)
        # 模拟“已落库区间被改坏”：把投影里的值改掉。
        target = next(
            row for row in store.rows("scd_effective_interval")
            if row["目标维度"] == dim and row["字段"] == field and row["生效开始"] == "2026-09-09"
        )
        target["值"] = "被误改"
        report = service.integrity(dim, store=store)
        assert report["ok"] is False
        assert any(item["问题"] == "字段归属/生效区间不一致" for item in report["drift"])

        repaired = service.repair(dim, store=store)
        assert repaired["repaired_fields"]
        assert service.integrity(dim, store=store)["ok"] is True

    def test_overlap_is_reported(self, store: Store, service: ScdHistoryService) -> None:
        dim, field = "DIM_PRODUCT", "product_category"
        service.append(dim, field=field, new_value="生鲜", business_date="2026-09-09",
                       record_type=PUBLISH, operator="publisher", store=store)
        # 人为注入一段与现有区间重叠的投影。
        store.rows("scd_effective_interval").append({
            "id": 9999, "区间ID": "EFF-BAD", "目标维度": dim, "字段": field,
            "值": "重叠", "生效开始": "2026-09-01", "生效结束": "2026-09-30",
            "来源履历ID": "HST-000001", "来源记录来源": PUBLISH,
        })
        report = service.integrity(dim, store=store)
        assert report["ok"] is False
        assert any(item["字段"] == field for item in report["overlaps"])

    def test_readonly_violation_detected(self, store: Store, service: ScdHistoryService) -> None:
        dim = "DIM_PRODUCT"
        result = service.append(dim, field="product_category", new_value="生鲜",
                                business_date="2026-09-09", record_type=PUBLISH,
                                operator="publisher", store=store)
        log_id = result["entry"]["关联日志ID"]
        log = store.find("slow_change_log", int(log_id))
        log["新值"] = "被篡改"
        report = service.integrity(dim, store=store)
        assert report["ok"] is False
        assert report["readonly_violations"]


class TestStrategyBehaviour:
    def test_scd_type0_rejects_changes(self, store: Store, service: ScdHistoryService) -> None:
        with pytest.raises(HistoryValidationError):
            service.append("DIM_TIME", field="date_full", new_value="2026-01-02",
                           business_date="2026-10-01", record_type=PUBLISH,
                           operator="publisher", store=store)

    def test_scd_type1_keeps_latest_only(self, store: Store, service: ScdHistoryService) -> None:
        dim, field = "DIM_STORE", "store_name"
        service.append(dim, field=field, old_value="东区", new_value="华东",
                       business_date="2026-10-01", record_type=PUBLISH,
                       operator="publisher", store=store)
        service.append(dim, field=field, old_value="华东", new_value="华南",
                       business_date="2026-10-15", record_type=PUBLISH,
                       operator="publisher", store=store)
        spans = _intervals(service, store, dim, field)
        assert len(spans) == 1
        assert spans[0]["值"] == "华南"
        assert spans[0]["SCD类型"] == "SCD类型1"


class TestChangeLogIntegration:
    def test_change_log_registration_writes_readonly_history(
        self, store: Store, service: ScdHistoryService
    ) -> None:
        from app.services.slow_change_log import SlowChangeLogService

        log_service = SlowChangeLogService().bind_store(store)
        entry, missing = log_service.create_entry({
            "目标维度": "DIM_PRODUCT",
            "变更字段": "product_name",
            "旧值": "旧名称",
            "新值": "新名称",
            "业务日期": "2026-09-12",
            "记录来源": PUBLISH,
            "登记人": "publisher",
            "变更原因": "商品改名",
        })
        assert missing == []
        assert entry["履历ID"]
        assert entry["是否只读"] is True
        history = service.history("DIM_PRODUCT", field="product_name", store=store)
        assert len(history) == 1
        assert history[0]["旧值"] == "旧名称"
        assert history[0]["新值"] == "新名称"
        assert history[0]["业务日期"] == "2026-09-12"
        assert history[0]["是否只读"] is True

    def test_trace_action_returns_effective_ownership(
        self, store: Store
    ) -> None:
        from app.services.slow_change_log import SlowChangeLogService

        log_service = SlowChangeLogService().bind_store(store)
        entry, _ = log_service.create_entry({
            "目标维度": "DIM_PRODUCT",
            "变更字段": "product_category",
            "新值": "生鲜",
            "业务日期": "2026-09-09",
            "记录来源": PUBLISH,
            "登记人": "publisher",
        })
        action_entry, message = log_service.run_action(entry["id"], "追溯变更")
        assert action_entry["status"] == "已追溯"
        assert "关联" in message

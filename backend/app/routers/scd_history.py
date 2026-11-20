"""SCD 变更履历与生效区间接口。

路径单独挂在 ``/api/scd-history`` 下，且在通用 ``/api/{module}`` 资源路由之前注册，
避免 ``/api/scd-history/...`` 被动态模块路由抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.scd_history import (
    CORRECTION,
    PUBLISH,
    HistoryError,
    scd_history_service,
)

router = APIRouter(prefix="/api/scd-history", tags=["SCD变更履历"])


class HistoryAppendPayload(BaseModel):
    """登记 / 补录一条履历。"""

    字段: str = Field(default="", description="变更字段")
    新值: str = Field(default="", description="新值")
    旧值: str = Field(default="", description="旧值；留空时按生效顺序自动补齐")
    业务日期: str = Field(default="", description="业务生效日期 YYYY-MM-DD")
    记录来源: str = Field(default=PUBLISH, description="首发发布 / 迟到补录 / 更正补录")
    更正来源: str = Field(default="", description="被更正的履历ID或变更ID")
    变更原因: str = Field(default="")
    登记人: str = Field(default="data_steward", description="维度维护人角色")
    期望版本: int | None = Field(default=None, description="乐观并发校验的履历版本号")

    # 兼容前端通用键名
    field: str = ""
    new_value: str | None = None
    old_value: str | None = None
    business_date: str = ""
    record_type: str = ""
    correction_source: str = ""
    reason: str = ""
    operator: str = ""
    expected_revision: int | None = None


class RepairPayload(BaseModel):
    登记人: str = Field(default="data_steward")
    operator: str = ""


def _call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except HistoryError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, **exc.details},
        ) from exc


@router.get("/dimensions")
def list_dimensions() -> dict[str, Any]:
    """列出可追溯的维度及其履历/区间概况。"""
    items = scd_history_service.dimensions()
    return {"items": items, "total": len(items)}


@router.get("/roles")
def list_roles() -> dict[str, Any]:
    """返回登记人角色与权限，供前端下拉和按钮显隐。"""
    from app.services.scd_history import ROLES

    return {"items": [{"key": key, **value} for key, value in ROLES.items()]}


@router.get("/{dim}/history")
def get_history(
    dim: str,
    field: str | None = Query(default=None),
    include_corrections: bool = Query(default=True),
) -> dict[str, Any]:
    """维度履历（只追加、只读）。"""
    page = scd_history_service.history_page(
        dim, field=field, include_corrections=include_corrections)
    page["total"] = len(page["items"])
    return page


@router.post("/{dim}/history")
def append_history(dim: str, payload: HistoryAppendPayload) -> dict[str, Any]:
    field = payload.字段 or payload.field
    new_value = payload.新值 if payload.新值 else (payload.new_value or "")
    old_value = payload.旧值 if payload.旧值 else (payload.old_value or "")
    business_date = payload.业务日期 or payload.business_date
    record_type = payload.记录来源 or payload.record_type or PUBLISH
    correction_source = payload.更正来源 or payload.correction_source
    reason = payload.变更原因 or payload.reason
    operator = payload.登记人 or payload.operator or "data_steward"
    expected = payload.期望版本 if payload.期望版本 is not None else payload.expected_revision
    return _call(
        scd_history_service.append,
        dim,
        field=field,
        new_value=new_value,
        old_value=old_value,
        business_date=business_date,
        record_type=record_type,
        correction_source=correction_source,
        reason=reason,
        operator=operator,
        expected_revision=expected,
    )


@router.get("/{dim}/intervals")
def get_intervals(dim: str, field: str | None = Query(default=None)) -> dict[str, Any]:
    """生效区间投影：每个字段的起止日期、取值和来源履历。"""
    scd_history_service.ensure(dim)
    items = scd_history_service.intervals(dim, field=field)
    return {"items": items, "total": len(items)}


@router.get("/{dim}/as-of")
def get_as_of(dim: str, date: str = Query(alias="date"), field: str | None = Query(default=None)) -> dict[str, Any]:
    """追溯指定日期：返回当日生效记录并指出关联更正。"""
    return _call(scd_history_service.as_of, dim, date, field=field)


@router.get("/{dim}/integrity")
def get_integrity(dim: str) -> dict[str, Any]:
    """校验投影与履历是否一致、区间是否重叠、原值是否只读。"""
    return _call(scd_history_service.integrity, dim)


@router.post("/{dim}/repair")
def repair(dim: str, payload: RepairPayload) -> dict[str, Any]:
    """按履历重算并整体替换投影（仅更正权限角色）。"""
    operator = payload.登记人 or payload.operator or "data_steward"
    return _call(scd_history_service.repair, dim, operator=operator)


@router.post("/{dim}/corrections")
def append_correction(dim: str, payload: HistoryAppendPayload) -> dict[str, Any]:
    """补录一条更正履历的语义化入口。"""
    payload.记录来源 = payload.记录来源 or CORRECTION
    return append_history(dim, payload)

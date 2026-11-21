"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class ReportMigrationPayload(BaseModel):
    report_numbers: list[str] | None = None
    stop_after: int | None = None
    timepoint: int | None = None


class ReportRecomputePayload(BaseModel):
    metrics: list[str] | None = None
    reviewer: str = ""


class ReportSignPayload(BaseModel):
    result: str = "通过"
    reviewer: str = ""
    remark: str = ""


class ReportImportPayload(BaseModel):
    csv_text: str
    stop_after: int | None = None

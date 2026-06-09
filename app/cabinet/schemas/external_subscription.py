"""Схемы для админ-управления внешними подписками в кабинете."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ExternalConfigItem(BaseModel):
    id: int
    name: str
    display_name: str | None = None
    protocol: str | None = None
    raw_link: str
    is_selected: bool
    is_active: bool
    last_seen_at: datetime | None = None

    class Config:
        from_attributes = True


class ConfigRenameRequest(BaseModel):
    display_name: str | None = Field(None, max_length=255)


class ExternalSourceListItem(BaseModel):
    id: int
    name: str
    url: str
    is_active: bool
    refresh_interval_minutes: int
    last_fetched_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    configs_count: int
    selected_count: int = 0

    class Config:
        from_attributes = True


class ExternalSourceListResponse(BaseModel):
    enabled: bool
    public_url: str | None = None
    total_selected: int = 0
    sources: list[ExternalSourceListItem]


class ExternalSourceDetailResponse(ExternalSourceListItem):
    configs: list[ExternalConfigItem] = Field(default_factory=list)


class CreateSourceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1)
    refresh_interval_minutes: int | None = Field(None, ge=5, le=10080)


class UpdateSourceRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    url: str | None = Field(None, min_length=1)
    refresh_interval_minutes: int | None = Field(None, ge=5, le=10080)
    is_active: bool | None = None


class SelectionRequest(BaseModel):
    selected_ids: list[int] = Field(default_factory=list)


class RefreshResponse(BaseModel):
    fetched: int = 0
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    error: str | None = None

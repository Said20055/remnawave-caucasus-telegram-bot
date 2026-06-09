"""Админ-роуты кабинета: управление внешними подписками (источники + выбор конфигов)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import external_subscription as crud
from app.database.models import User
from app.services import external_subscription_service as ext_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.external_subscription import (
    ConfigRenameRequest,
    CreateSourceRequest,
    ExternalConfigItem,
    ExternalSourceDetailResponse,
    ExternalSourceListItem,
    ExternalSourceListResponse,
    RefreshResponse,
    SelectionRequest,
    UpdateSourceRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/external-subscriptions', tags=['Admin External Subscriptions'])


async def _list_item(db: AsyncSession, source) -> ExternalSourceListItem:
    total, selected = await crud.count_configs_by_source(db, source.id)
    return ExternalSourceListItem(
        id=source.id,
        name=source.name,
        url=source.url,
        is_active=source.is_active,
        refresh_interval_minutes=source.refresh_interval_minutes,
        last_fetched_at=source.last_fetched_at,
        last_status=source.last_status,
        last_error=source.last_error,
        configs_count=total,
        selected_count=selected,
    )


@router.get('', response_model=ExternalSourceListResponse)
async def list_sources(
    admin: User = Depends(require_permission('servers:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    sources = await crud.list_sources(db)
    items = [await _list_item(db, s) for s in sources]
    total_selected = await crud.count_selected_active(db)
    return ExternalSourceListResponse(
        enabled=settings.is_external_subscriptions_enabled(),
        public_url=settings.get_external_sub_public_url(),
        total_selected=total_selected,
        sources=items,
    )


async def _detail(db: AsyncSession, source) -> ExternalSourceDetailResponse:
    base = await _list_item(db, source)
    configs = await crud.get_configs_by_source(db, source.id)
    return ExternalSourceDetailResponse(
        **base.model_dump(),
        configs=[ExternalConfigItem.model_validate(c) for c in configs],
    )


@router.post('', response_model=ExternalSourceDetailResponse)
async def create_source(
    payload: CreateSourceRequest,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    interval = payload.refresh_interval_minutes or settings.EXTERNAL_SUB_DEFAULT_REFRESH_MINUTES
    source = await crud.create_source(
        db, name=payload.name, url=payload.url, refresh_interval_minutes=interval
    )
    # Немедленный фетч, чтобы админ сразу увидел конфиги для выбора
    await ext_service.refresh_source(db, source)
    await db.refresh(source)
    logger.info('Admin created external subscription source', admin_id=admin.id, source_id=source.id)
    return await _detail(db, source)


@router.get('/{source_id}', response_model=ExternalSourceDetailResponse)
async def get_source(
    source_id: int,
    admin: User = Depends(require_permission('servers:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    return await _detail(db, source)


@router.patch('/{source_id}', response_model=ExternalSourceDetailResponse)
async def update_source(
    source_id: int,
    payload: UpdateSourceRequest,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    await crud.update_source(db, source, **payload.model_dump(exclude_unset=True))
    return await _detail(db, source)


@router.delete('/{source_id}')
async def delete_source(
    source_id: int,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    await crud.delete_source(db, source)
    logger.info('Admin deleted external subscription source', admin_id=admin.id, source_id=source_id)
    return {'message': 'Source deleted'}


@router.post('/{source_id}/refresh', response_model=RefreshResponse)
async def refresh_source(
    source_id: int,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    stats = await ext_service.refresh_source(db, source)
    return RefreshResponse(**{k: v for k, v in stats.items() if k in RefreshResponse.model_fields})


@router.patch('/{source_id}/configs/{config_id}', response_model=ExternalSourceDetailResponse)
async def rename_config(
    source_id: int,
    config_id: int,
    payload: ConfigRenameRequest,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Задаёт кастомное отображаемое имя конфигу (видят пользователи). Пусто → имя из источника."""
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    updated = await crud.set_config_display_name(db, config_id, payload.display_name)
    if updated is None or updated.source_id != source_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Config not found')
    return await _detail(db, source)


@router.put('/{source_id}/selection', response_model=ExternalSourceDetailResponse)
async def set_selection(
    source_id: int,
    payload: SelectionRequest,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    source = await crud.get_source(db, source_id)
    if not source:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Source not found')
    await crud.set_source_selection(db, source_id, payload.selected_ids)
    return await _detail(db, source)

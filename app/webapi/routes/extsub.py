"""Публичная выдача внешней подписки (без авторизации, защита — секретный токен в пути).

GET /extsub/{token} → base64-список выбранных внешних конфигов (формат Happ/v2ray).
Через единый веб-сервер доступно как https://<host>/api/extsub/<token>.
"""

from __future__ import annotations

import base64

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription
from app.services import external_subscription_service as ext_service

from ..dependencies import get_db_session


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/extsub/{token}', include_in_schema=False)
async def serve_external_subscription(
    token: str = Path(..., min_length=1),
    db: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    configured = (settings.EXTERNAL_SUB_TOKEN or '').strip()
    # 404 (а не 401/403), чтобы не раскрывать существование эндпоинта при неверном токене
    if not settings.is_external_subscriptions_enabled() or not configured or token != configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    body = await ext_service.build_external_subscription_b64(db)

    title_b64 = base64.b64encode((settings.EXTERNAL_SUB_PROFILE_TITLE or 'Extra').encode('utf-8')).decode('utf-8')
    headers = {
        'profile-title': f'base64:{title_b64}',
        'profile-update-interval': str(settings.EXTERNAL_SUB_PROFILE_UPDATE_HOURS or 12),
        'content-disposition': 'inline',
    }
    return PlainTextResponse(content=body, media_type='text/plain; charset=utf-8', headers=headers)


@router.get('/sub/{short_uuid}', include_in_schema=False)
async def serve_merged_subscription(
    short_uuid: str = Path(..., min_length=1),
    user_agent: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    """Персональная слитая подписка: конфиги Remnawave пользователя + выбранные внешние.

    Бот выступает точкой выдачи: тянет подписку пользователя из панели (по его
    subscription_url / short_uuid), подмешивает внешние конфиги и отдаёт клиенту,
    проксируя заголовки трафика/срока от Remnawave.
    """
    if not settings.is_merged_subscription_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    result = await db.execute(
        select(Subscription).where(Subscription.remnawave_short_uuid == short_uuid)
    )
    subscription = result.scalar_one_or_none()
    remnawave_url = getattr(subscription, 'subscription_url', None) if subscription else None
    if not subscription or not remnawave_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        rw_status, merged_body, proxied = await ext_service.build_merged_subscription(
            db, remnawave_url, user_agent
        )
    except Exception as e:
        logger.warning('Ошибка сборки слитой подписки', short_uuid=short_uuid, error=str(e))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY) from e

    if rw_status >= 400:
        # Прокидываем статус панели (напр. 404 для отозванной подписки)
        return PlainTextResponse(content='', status_code=rw_status)

    # Заголовки трафика/срока от панели + дефолтный интервал обновления, если панель его не дала
    headers = dict(proxied)
    headers.setdefault('profile-update-interval', str(settings.EXTERNAL_SUB_PROFILE_UPDATE_HOURS or 12))
    headers['content-disposition'] = 'inline'
    return PlainTextResponse(content=merged_body, media_type='text/plain; charset=utf-8', headers=headers)

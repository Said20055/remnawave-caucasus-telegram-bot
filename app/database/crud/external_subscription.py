"""CRUD для внешних подписок (источники + извлечённые конфиги)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ExternalConfig, ExternalSubscriptionSource


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


async def create_source(
    db: AsyncSession,
    *,
    name: str,
    url: str,
    headers: dict | None = None,
    refresh_interval_minutes: int = 360,
    is_active: bool = True,
) -> ExternalSubscriptionSource:
    source = ExternalSubscriptionSource(
        name=name,
        url=url,
        headers=headers,
        refresh_interval_minutes=refresh_interval_minutes,
        is_active=is_active,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


async def get_source(db: AsyncSession, source_id: int) -> ExternalSubscriptionSource | None:
    result = await db.execute(
        select(ExternalSubscriptionSource).where(ExternalSubscriptionSource.id == source_id)
    )
    return result.scalar_one_or_none()


async def list_sources(db: AsyncSession) -> list[ExternalSubscriptionSource]:
    result = await db.execute(
        select(ExternalSubscriptionSource).order_by(ExternalSubscriptionSource.id.asc())
    )
    return list(result.scalars().all())


async def list_active_sources(db: AsyncSession) -> list[ExternalSubscriptionSource]:
    result = await db.execute(
        select(ExternalSubscriptionSource)
        .where(ExternalSubscriptionSource.is_active == True)
        .order_by(ExternalSubscriptionSource.id.asc())
    )
    return list(result.scalars().all())


async def update_source(db: AsyncSession, source: ExternalSubscriptionSource, **fields) -> ExternalSubscriptionSource:
    allowed = {'name', 'url', 'headers', 'refresh_interval_minutes', 'is_active'}
    for key, value in fields.items():
        if key in allowed and value is not None:
            setattr(source, key, value)
    await db.commit()
    await db.refresh(source)
    return source


async def delete_source(db: AsyncSession, source: ExternalSubscriptionSource) -> None:
    await db.delete(source)
    await db.commit()


async def mark_source_fetched(
    db: AsyncSession,
    source: ExternalSubscriptionSource,
    *,
    status: str,
    error: str | None = None,
    configs_count: int | None = None,
) -> None:
    source.last_fetched_at = datetime.now(UTC)
    source.last_status = status
    source.last_error = error
    if configs_count is not None:
        source.configs_count = configs_count
    await db.commit()


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------


async def get_configs_by_source(
    db: AsyncSession, source_id: int, *, only_active: bool = False
) -> list[ExternalConfig]:
    stmt = select(ExternalConfig).where(ExternalConfig.source_id == source_id)
    if only_active:
        stmt = stmt.where(ExternalConfig.is_active == True)
    stmt = stmt.order_by(ExternalConfig.id.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def upsert_config(
    db: AsyncSession,
    *,
    source_id: int,
    remote_key: str,
    name: str,
    raw_link: str,
    protocol: str | None,
) -> tuple[ExternalConfig, bool]:
    """Создаёт или обновляет конфиг по (source_id, remote_key). Возвращает (config, created).

    Существующий конфиг реактивируется и получает свежий raw_link/name; флаг is_selected
    при этом сохраняется (выбор админа не сбрасывается при обновлении источника).
    Не коммитит — вызывающий код коммитит пачкой.
    """
    result = await db.execute(
        select(ExternalConfig).where(
            ExternalConfig.source_id == source_id,
            ExternalConfig.remote_key == remote_key,
        )
    )
    config = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if config is None:
        config = ExternalConfig(
            source_id=source_id,
            remote_key=remote_key,
            name=name,
            raw_link=raw_link,
            protocol=protocol,
            is_selected=False,
            is_active=True,
            last_seen_at=now,
        )
        db.add(config)
        return config, True

    config.name = name
    config.raw_link = raw_link
    config.protocol = protocol
    config.is_active = True
    config.last_seen_at = now
    return config, False


async def deactivate_missing_configs(
    db: AsyncSession, source_id: int, seen_remote_keys: set[str]
) -> int:
    """Помечает is_active=false конфиги источника, которых не было в свежем фетче. Не коммитит."""
    configs = await get_configs_by_source(db, source_id)
    deactivated = 0
    for cfg in configs:
        if cfg.remote_key not in seen_remote_keys and cfg.is_active:
            cfg.is_active = False
            deactivated += 1
    return deactivated


async def set_config_display_name(
    db: AsyncSession, config_id: int, display_name: str | None
) -> ExternalConfig | None:
    """Задаёт/сбрасывает кастомное имя конфига. Пустая строка → NULL (имя из источника)."""
    result = await db.execute(select(ExternalConfig).where(ExternalConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        return None
    name = (display_name or '').strip()
    config.display_name = name or None
    await db.commit()
    await db.refresh(config)
    return config


async def set_config_selection(db: AsyncSession, config_id: int, is_selected: bool) -> ExternalConfig | None:
    result = await db.execute(select(ExternalConfig).where(ExternalConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        return None
    config.is_selected = is_selected
    await db.commit()
    await db.refresh(config)
    return config


async def set_source_selection(db: AsyncSession, source_id: int, selected_ids: list[int]) -> int:
    """Массово выставляет выбор для конфигов источника: id из списка → selected, остальные → нет."""
    selected = set(selected_ids)
    configs = await get_configs_by_source(db, source_id)
    changed = 0
    for cfg in configs:
        want = cfg.id in selected
        if cfg.is_selected != want:
            cfg.is_selected = want
            changed += 1
    await db.commit()
    return changed


async def get_selected_active_configs(db: AsyncSession) -> list[tuple[str, str | None]]:
    """(raw_link, display_name) всех выбранных и активных конфигов (для выдачи подписки)."""
    result = await db.execute(
        select(ExternalConfig.raw_link, ExternalConfig.display_name)
        .join(ExternalSubscriptionSource, ExternalConfig.source_id == ExternalSubscriptionSource.id)
        .where(
            ExternalConfig.is_selected == True,
            ExternalConfig.is_active == True,
            ExternalSubscriptionSource.is_active == True,
        )
        .order_by(ExternalConfig.source_id.asc(), ExternalConfig.id.asc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def get_selected_active_links(db: AsyncSession) -> list[str]:
    """Сырые ссылки выбранных активных конфигов с применённым кастомным именем (display_name)."""
    from app.services.external_subscription_service import apply_display_name

    configs = await get_selected_active_configs(db)
    return [apply_display_name(raw, dn) for raw, dn in configs]


async def count_selected_active(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(ExternalConfig)
        .join(ExternalSubscriptionSource, ExternalConfig.source_id == ExternalSubscriptionSource.id)
        .where(
            ExternalConfig.is_selected == True,
            ExternalConfig.is_active == True,
            ExternalSubscriptionSource.is_active == True,
        )
    )
    return int(result.scalar() or 0)


async def count_configs_by_source(db: AsyncSession, source_id: int) -> tuple[int, int]:
    """Возвращает (всего, выбрано) для источника."""
    total_res = await db.execute(
        select(func.count()).select_from(ExternalConfig).where(ExternalConfig.source_id == source_id)
    )
    sel_res = await db.execute(
        select(func.count())
        .select_from(ExternalConfig)
        .where(ExternalConfig.source_id == source_id, ExternalConfig.is_selected == True)
    )
    return int(total_res.scalar() or 0), int(sel_res.scalar() or 0)


async def touch_selected_configs_count(db: AsyncSession, source_id: int) -> None:
    """Обновляет configs_count источника = число активных конфигов."""
    res = await db.execute(
        select(func.count())
        .select_from(ExternalConfig)
        .where(ExternalConfig.source_id == source_id, ExternalConfig.is_active == True)
    )
    count = int(res.scalar() or 0)
    await db.execute(
        update(ExternalSubscriptionSource)
        .where(ExternalSubscriptionSource.id == source_id)
        .values(configs_count=count)
    )

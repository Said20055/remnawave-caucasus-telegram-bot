"""Сервис внешних подписок: фетч чужого подписочного URL, парсинг конфигов,
периодическое обновление и сборка итоговой base64-подписки для выдачи.

Идея и парсинг во многом повторяют пользовательский external_vpn_service,
но переписаны под async (aiohttp) и модели Bedolaga.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import aiohttp
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import external_subscription as crud
from app.database.models import ExternalSubscriptionSource


logger = structlog.get_logger(__name__)

VALID_PREFIXES = ('vless://', 'vmess://', 'trojan://', 'ss://', 'hysteria2://', 'hy2://', 'tuic://')

_HWID = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'bedolaga-ext-sub-fetcher'))

DEFAULT_HEADERS = {
    'User-Agent': 'HappProxy/2.1.6 (Linux; Bot)',
    'x-hwid': _HWID,
    'x-device-os': 'Linux',
    'x-ver-os': '6.1',
    'x-device-model': 'Server',
    'Accept': '*/*',
}

_FETCH_TIMEOUT_SECONDS = 20


def _protocol_of(link: str) -> str | None:
    for prefix in VALID_PREFIXES:
        if link.startswith(prefix):
            return prefix[:-3]  # strip '://'
    return None


def _host_port_of(link: str, protocol: str) -> str:
    """Best-effort host:port извлекается для стабильного remote_key."""
    try:
        if protocol == 'vmess':
            # vmess:// + base64(json)
            payload = link[len('vmess://') :]
            import json

            decoded = base64.b64decode(payload + '==').decode('utf-8', errors='ignore')
            data = json.loads(decoded)
            return f'{data.get("add", "")}:{data.get("port", "")}'
        parsed = urlsplit(link)
        host = parsed.hostname or ''
        port = parsed.port or ''
        if host:
            return f'{host}:{port}'
    except Exception:
        pass
    return ''


def _remote_key(raw_link: str, protocol: str | None, name: str) -> str:
    """Стабильный ключ конфига внутри источника: protocol|host:port|name.

    Если host:port извлечь не удалось — берём хеш самой ссылки (best-effort).
    Ограничен 255 символами под колонку.
    """
    proto = protocol or 'unknown'
    host_port = _host_port_of(raw_link, proto) if protocol else ''
    if host_port:
        key = f'{proto}|{host_port}|{name}'.strip()
    else:
        digest = hashlib.sha1(raw_link.encode('utf-8')).hexdigest()
        key = f'{proto}|{digest}'
    return key[:255]


def _extract_links(text: str) -> list[dict]:
    links: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        protocol = _protocol_of(line)
        if not protocol:
            continue
        if '#' in line:
            _, fragment = line.rsplit('#', 1)
            name = unquote(fragment).strip() or line[:40]
        else:
            name = line[:40]
        links.append({'name': name, 'raw_link': line, 'protocol': protocol})
    return links


def parse_subscription(content: str) -> list[dict]:
    """Plain-text или base64 подписка → список {name, raw_link, protocol}."""
    links = _extract_links(content)
    if links:
        return links
    try:
        decoded = base64.b64decode(content.strip() + '==').decode('utf-8', errors='ignore')
        return _extract_links(decoded)
    except Exception:
        return []


def _headers_for(source: ExternalSubscriptionSource | None) -> dict:
    if source is not None and isinstance(source.headers, dict) and source.headers:
        return source.headers
    return DEFAULT_HEADERS


async def fetch_subscription_text(url: str, headers: dict | None = None) -> str:
    """Фетчит подписочный URL и возвращает тело (str)."""
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(url, headers=headers or DEFAULT_HEADERS, allow_redirects=True) as resp:
            resp.raise_for_status()
            return await resp.text()


async def fetch_and_parse(url: str, headers: dict | None = None) -> list[dict]:
    """Фетч + парс. Возвращает список {name, raw_link, protocol} (без сохранения в БД)."""
    text = await fetch_subscription_text(url, headers)
    return parse_subscription(text)


async def refresh_source(db: AsyncSession, source: ExternalSubscriptionSource) -> dict:
    """Обновляет конфиги источника: фетч → upsert по remote_key → деактивация исчезнувших.

    Сохранённый выбор (is_selected) не сбрасывается. Возвращает статистику.
    """
    stats = {'fetched': 0, 'created': 0, 'updated': 0, 'deactivated': 0, 'error': None}
    try:
        items = await fetch_and_parse(source.url, _headers_for(source))
    except Exception as e:
        logger.warning('Ошибка фетча внешней подписки', source_id=source.id, url=source.url, error=str(e))
        await crud.mark_source_fetched(db, source, status='error', error=str(e)[:500])
        stats['error'] = str(e)
        return stats

    seen_keys: set[str] = set()
    for item in items:
        key = _remote_key(item['raw_link'], item.get('protocol'), item['name'])
        if key in seen_keys:
            continue  # дубликат внутри одного фетча
        seen_keys.add(key)
        _, created = await crud.upsert_config(
            db,
            source_id=source.id,
            remote_key=key,
            name=item['name'],
            raw_link=item['raw_link'],
            protocol=item.get('protocol'),
        )
        if created:
            stats['created'] += 1
        else:
            stats['updated'] += 1

    stats['fetched'] = len(seen_keys)
    stats['deactivated'] = await crud.deactivate_missing_configs(db, source.id, seen_keys)
    await crud.touch_selected_configs_count(db, source.id)
    await db.commit()
    await crud.mark_source_fetched(db, source, status='ok', error=None, configs_count=len(seen_keys))
    logger.info('Внешняя подписка обновлена', source_id=source.id, **{k: v for k, v in stats.items() if k != 'error'})
    return stats


async def refresh_due_sources(db: AsyncSession) -> dict:
    """Обновляет все активные источники, у которых истёк интервал. Для monitoring_service."""
    if not settings.is_external_subscriptions_enabled():
        return {'skipped': True, 'reason': 'disabled'}

    now = datetime.now(UTC)
    sources = await crud.list_active_sources(db)
    processed = 0
    for source in sources:
        interval = source.refresh_interval_minutes or settings.EXTERNAL_SUB_DEFAULT_REFRESH_MINUTES
        last = source.last_fetched_at
        if last is not None and (now - last) < timedelta(minutes=interval):
            continue
        await refresh_source(db, source)
        processed += 1
    return {'sources': len(sources), 'refreshed': processed}


async def build_external_subscription_b64(db: AsyncSession) -> str:
    """Собирает итоговую подписку: base64 от \\n-списка выбранных активных ссылок."""
    links = await crud.get_selected_active_links(db)
    plain = '\n'.join(links)
    return base64.b64encode(plain.encode('utf-8')).decode('utf-8')


# ---------------------------------------------------------------------------
# Слитая подписка: Remnawave пользователя + внешние конфиги
# ---------------------------------------------------------------------------

# Заголовки ответа Remnawave, которые нужно пробросить клиенту (трафик/срок/профиль)
_PROXY_RESPONSE_HEADERS = (
    'subscription-userinfo',
    'profile-update-interval',
    'profile-title',
    'profile-web-page-url',
    'support-url',
    'announce',
)


async def fetch_remnawave_subscription(url: str, user_agent: str | None) -> tuple[int, str, dict[str, str]]:
    """Серверный фетч подписки Remnawave пользователя. Форвардит User-Agent клиента,
    чтобы панель вернула формат под конкретный клиент (Happ/clash/...).

    Returns: (status_code, body_text, proxied_headers).
    """
    headers = {'Accept': '*/*'}
    if user_agent:
        headers['User-Agent'] = user_agent
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            body = await resp.text()
            proxied = {k: v for k, v in resp.headers.items() if k.lower() in _PROXY_RESPONSE_HEADERS}
            return resp.status, body, proxied


def merge_remnawave_with_external(remnawave_body: str, external_links: list[str]) -> str:
    """Сливает контент Remnawave с внешними ссылками в base64-список (формат Happ/v2ray).

    Если тело Remnawave — это список ссылок (base64 или plaintext), внешние добавляются в конец
    и всё перекодируется в base64. Если формат не распознан (clash/singbox YAML/JSON) — возвращаем
    тело как есть, чтобы не сломать клиент (внешние в таких форматах не подмешиваем).
    """
    rw_items = parse_subscription(remnawave_body)
    if not rw_items:
        # Не список ссылок — отдаём как есть (passthrough), внешние не трогаем
        return remnawave_body
    rw_links = [item['raw_link'] for item in rw_items]
    combined = rw_links + [link for link in external_links if link]
    return base64.b64encode('\n'.join(combined).encode('utf-8')).decode('utf-8')


async def build_merged_subscription(
    db: AsyncSession, remnawave_url: str, user_agent: str | None
) -> tuple[int, str, dict[str, str]]:
    """Фетчит подписку Remnawave пользователя и подмешивает выбранные внешние конфиги.

    Returns: (status_code, merged_body, proxied_headers).
    """
    status, body, proxied = await fetch_remnawave_subscription(remnawave_url, user_agent)
    if status >= 400:
        return status, body, proxied
    external_links = await crud.get_selected_active_links(db)
    merged = merge_remnawave_with_external(body, external_links)
    return status, merged, proxied

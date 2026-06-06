"""Autopay settings endpoint.

PATCH /subscription/autopay
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import AutopayUpdateRequest


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.patch('/autopay')
async def update_autopay(
    request: AutopayUpdateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Update autopay settings."""
    from .helpers import resolve_subscription

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if request.enabled:
        # Classic subscriptions cannot use autopay when tariff mode is enabled
        from app.config import settings

        if settings.is_tariffs_mode() and not subscription.tariff_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for classic subscriptions. Please purchase a tariff.',
            )

        # Триальные подписки — пробник, автопродление не имеет смысла
        # NULL-safe: is_trial can be None in legacy rows — treat as trial
        if subscription.is_trial is not False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for trial subscriptions',
            )

        # Суточные подписки имеют свой механизм продления (DailySubscriptionService),
        # глобальный autopay для них запрещён
        await db.refresh(subscription, ['tariff'])
        if subscription.tariff and getattr(subscription.tariff, 'is_daily', False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for daily subscriptions',
            )

    subscription.autopay_enabled = request.enabled

    if request.days_before is not None:
        subscription.autopay_days_before = request.days_before

    await db.commit()

    return {
        'message': 'Autopay settings updated',
        'autopay_enabled': subscription.autopay_enabled,
        'autopay_days_before': subscription.autopay_days_before,
    }


@router.post('/autopay/test')
async def diagnose_autopay(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
    force_charge: bool = Query(
        False,
        description='If true, actually attempt to charge the saved card (real money). '
        'If false, only run a dry-run diagnostic.',
    ),
):
    """Диагностика автоплатежа: прогоняет реальные проверки рекуррентного списания
    для подписки пользователя и возвращает по шагам, почему автоплатёж пройдёт/не пройдёт.

    При ``force_charge=true`` дополнительно пытается реально списать с сохранённой карты
    (как это делает фоновый ``recurrent_payment_service``).
    """
    from datetime import UTC, datetime

    from app.config import settings
    from app.database.crud.saved_payment_method import get_active_payment_methods_by_user
    from app.database.crud.user import lock_user_for_pricing
    from app.database.models import SubscriptionStatus
    from app.services.pricing_engine import pricing_engine
    from app.services.recurrent_payment_service import resolve_renewal_target

    from .helpers import resolve_subscription

    steps: list[dict] = []

    def step(key: str, ok: bool | None, message: str, **detail):
        entry = {'key': key, 'ok': ok, 'message': message, 'detail': detail or None}
        steps.append(entry)
        logger.info('test_autopay step', step=key, ok=ok, message=message, user_id=user.id, **detail)
        return entry

    # 1. Глобальные флаги
    flags_ok = (
        settings.YOOKASSA_RECURRENT_ENABLED and settings.YOOKASSA_ENABLED and settings.ENABLE_AUTOPAY
    )
    step(
        'config_flags',
        bool(flags_ok),
        'Глобальные флаги автоплатежа',
        YOOKASSA_RECURRENT_ENABLED=settings.YOOKASSA_RECURRENT_ENABLED,
        YOOKASSA_ENABLED=settings.YOOKASSA_ENABLED,
        ENABLE_AUTOPAY=settings.ENABLE_AUTOPAY,
        YOOKASSA_MIN_AMOUNT_KOPEKS=settings.YOOKASSA_MIN_AMOUNT_KOPEKS,
        DEFAULT_AUTOPAY_DAYS_BEFORE=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
    )

    # 2. Подписка
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        step('subscription', False, 'Подписка не найдена')
        return {'verdict': 'no_subscription', 'force_charge': force_charge, 'steps': steps}
    await db.refresh(subscription, ['tariff'])
    days_until_expiry = (subscription.end_date - datetime.now(UTC)).total_seconds() / 86400
    step(
        'subscription',
        True,
        'Подписка найдена',
        subscription_id=subscription.id,
        status=subscription.status,
        is_trial=subscription.is_trial,
        tariff_id=subscription.tariff_id,
        tariff_name=getattr(subscription.tariff, 'name', None),
        end_date=subscription.end_date.isoformat() if subscription.end_date else None,
        days_until_expiry=round(days_until_expiry, 3),
        balance_kopeks=user.balance_kopeks,
    )

    # 3. autopay_enabled
    step('autopay_enabled', bool(subscription.autopay_enabled), 'Автоплатёж включён на подписке')

    # 4. Не триал
    step('not_trial', subscription.is_trial is False, 'Подписка не триальная (триалы пропускаются)')

    # 5. Не суточный тариф
    is_daily = bool(getattr(subscription.tariff, 'is_daily', False))
    step('not_daily', not is_daily, 'Тариф не суточный (у суточных свой механизм)')

    # 6. Целевой тариф продления (интро → целевой)
    target_tariff, autopay_period = await resolve_renewal_target(db, subscription)
    is_transition = target_tariff is not None and target_tariff.id != subscription.tariff_id
    step(
        'renewal_target',
        True,
        'Тариф и период для продления',
        target_tariff_id=getattr(target_tariff, 'id', None),
        target_tariff_name=getattr(target_tariff, 'name', None),
        period_days=autopay_period,
        is_transition=is_transition,
    )

    # 7. Стоимость продления / нехватка баланса (та же ветка, что в recurrent_payment_service)
    renewal_cost = None
    try:
        locked_user = await lock_user_for_pricing(db, user.id)
        if is_transition:
            pricing = await pricing_engine.calculate_tariff_purchase_price(
                target_tariff,
                autopay_period,
                device_limit=subscription.device_limit or 0,
                user=locked_user,
            )
        else:
            pricing = await pricing_engine.calculate_renewal_price(
                db, subscription, autopay_period, user=locked_user
            )
        renewal_cost = pricing.final_total
        balance = locked_user.balance_kopeks
        shortage = renewal_cost - balance
        step(
            'pricing',
            renewal_cost > 0,
            'Стоимость продления рассчитана',
            renewal_cost_kopeks=renewal_cost,
            balance_kopeks=balance,
            shortage_kopeks=shortage,
        )
    except Exception as e:
        step('pricing', False, f'Ошибка расчёта стоимости: {e}')
        return {'verdict': 'pricing_error', 'force_charge': force_charge, 'steps': steps}

    # 8. Хватает ли уже баланса (тогда карта не нужна — спишет обычный autopay)
    balance_sufficient = shortage <= 0
    step(
        'balance_check',
        True,
        'Баланса достаточно — спишет обычный autopay без карты'
        if balance_sufficient
        else 'Баланса не хватает — нужна сохранённая карта',
        balance_sufficient=balance_sufficient,
    )

    # 9. Срок: попадает ли в окно autopay_days_before
    days_before = getattr(subscription, 'autopay_days_before', None) or settings.DEFAULT_AUTOPAY_DAYS_BEFORE
    is_expired = subscription.status == SubscriptionStatus.EXPIRED.value
    within_window = days_until_expiry <= days_before or is_expired
    step(
        'expiry_window',
        within_window,
        'Подписка в окне автопродления'
        if within_window
        else f'Ещё рано: до истечения {round(days_until_expiry, 2)} дн., окно {days_before} дн.',
        autopay_days_before=days_before,
        is_expired=is_expired,
    )

    # 10. Сохранённые карты
    saved_methods = await get_active_payment_methods_by_user(db, user.id)
    step(
        'saved_cards',
        len(saved_methods) > 0,
        f'Сохранённых карт: {len(saved_methods)}',
        cards=[
            {
                'id': m.id,
                'last4': m.card_last4,
                'type': m.card_type,
                'yookassa_payment_method_id': m.yookassa_payment_method_id,
            }
            for m in saved_methods
        ],
    )

    # Итоговый вердикт сухого прогона
    would_charge = bool(
        flags_ok
        and subscription.autopay_enabled
        and subscription.is_trial is False
        and not is_daily
        and not balance_sufficient
        and within_window
        and saved_methods
    )
    topup_kopeks = max(shortage, settings.YOOKASSA_MIN_AMOUNT_KOPEKS) if not balance_sufficient else 0

    result: dict = {
        'verdict': 'would_charge' if would_charge else 'would_not_charge',
        'force_charge': force_charge,
        'would_topup_kopeks': topup_kopeks if would_charge else 0,
        'steps': steps,
    }

    # 11. Реальное списание (только при force_charge)
    if force_charge:
        if not (subscription.autopay_enabled and not is_daily and subscription.is_trial is False):
            step('charge', False, 'Списание не выполнено: подписка не подходит (триал/суточный/autopay off)')
            result['charge_result'] = 'precondition_failed'
            return result
        if balance_sufficient:
            step('charge', None, 'Списание не требуется: баланса уже достаточно')
            result['charge_result'] = 'balance_sufficient'
            return result
        if not saved_methods:
            step('charge', False, 'Списание невозможно: нет сохранённой карты')
            result['charge_result'] = 'no_card'
            return result

        from app.services.payment_service import PaymentService

        payment_service = PaymentService()
        yk = payment_service.yookassa_service
        if not yk or not getattr(yk, 'configured', False):
            step('charge', False, 'YooKassa сервис не сконфигурирован')
            result['charge_result'] = 'yookassa_not_configured'
            return result

        topup_rubles = topup_kopeks / 100
        description = settings.get_balance_payment_description(
            topup_kopeks, telegram_user_id=user.telegram_id, user_db_id=user.id
        )
        metadata = {
            'user_id': str(user.id),
            'user_telegram_id': str(user.telegram_id) if user.telegram_id else '',
            'purpose': 'recurrent_topup',
            'subscription_id': str(subscription.id),
            'source': 'cabinet_test_autopay',
        }
        today = datetime.now(UTC).strftime('%Y-%m-%d')
        charge_outcome = 'all_cards_failed'
        for m in saved_methods:
            idem_key = f'test_autopay_{subscription.id}_{m.id}_{today}'
            try:
                charge = await yk.create_autopayment(
                    amount=topup_rubles,
                    currency='RUB',
                    description=description,
                    payment_method_id=m.yookassa_payment_method_id,
                    metadata=metadata,
                    idempotence_key=idem_key,
                )
            except Exception as e:
                step('charge_attempt', False, f'Ошибка списания с карты *{m.card_last4}: {e}', card_id=m.id)
                continue
            if not charge:
                step('charge_attempt', False, f'YooKassa отклонил списание с карты *{m.card_last4}', card_id=m.id)
                continue
            step(
                'charge_attempt',
                True,
                f'Платёж создан на карту *{m.card_last4}',
                card_id=m.id,
                yookassa_payment_id=charge.get('id'),
                status=charge.get('status'),
                paid=charge.get('paid'),
                amount_kopeks=topup_kopeks,
            )
            charge_outcome = 'created'
            result['yookassa_payment'] = {
                'id': charge.get('id'),
                'status': charge.get('status'),
                'paid': charge.get('paid'),
                'amount_kopeks': topup_kopeks,
            }
            break
        result['charge_result'] = charge_outcome

    return result

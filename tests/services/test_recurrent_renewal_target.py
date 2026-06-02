"""Тесты выбора целевого тарифа авто-перехода (интро → целевой)."""

import app.database.crud.tariff as tariff_crud
from app.services.recurrent_payment_service import resolve_renewal_target


class FakeTariff:
    def __init__(self, id: int, shortest: int | None, next_tariff_id: int | None = None):
        self.id = id
        self._shortest = shortest
        self.next_tariff_id = next_tariff_id

    def get_shortest_period(self):
        return self._shortest


class FakeSubscription:
    def __init__(self, tariff: FakeTariff | None):
        self.tariff = tariff
        self.tariff_id = tariff.id if tariff else None


async def test_resolve_renewal_target_no_tariff():
    """Классическая подписка без тарифа → (None, 30)."""
    target, period = await resolve_renewal_target(None, FakeSubscription(None))
    assert target is None
    assert period == 30


async def test_resolve_renewal_target_same_tariff():
    """Обычный тариф без next_tariff_id продлевается сам собой."""
    tariff = FakeTariff(id=1, shortest=7)
    target, period = await resolve_renewal_target(None, FakeSubscription(tariff))
    assert target is tariff
    assert period == 7


async def test_resolve_renewal_target_transition(monkeypatch):
    """Интро-тариф с next_tariff_id → продление по целевому тарифу и его периоду."""
    intro = FakeTariff(id=1, shortest=7, next_tariff_id=2)
    target_tariff = FakeTariff(id=2, shortest=30)

    async def fake_get_tariff_by_id(db, tariff_id, **kwargs):
        assert tariff_id == 2
        return target_tariff

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', fake_get_tariff_by_id)

    target, period = await resolve_renewal_target(None, FakeSubscription(intro))
    assert target is target_tariff
    assert period == 30


async def test_resolve_renewal_target_missing_next_falls_back(monkeypatch):
    """Если целевой тариф не найден — продлеваем по текущему (без падения)."""
    intro = FakeTariff(id=1, shortest=7, next_tariff_id=99)

    async def fake_get_tariff_by_id(db, tariff_id, **kwargs):
        return None

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', fake_get_tariff_by_id)

    target, period = await resolve_renewal_target(None, FakeSubscription(intro))
    assert target is intro
    assert period == 7

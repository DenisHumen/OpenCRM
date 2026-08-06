"""Одинаковое значение в уникальном поле — отказ с объяснением, а не 500.

Между «проверили, что такого ещё нет» и «вставили» всегда есть окно, и попадают
в него не злоумышленники, а обычная жизнь: двойное нажатие, две вкладки, двое
за соседними столами. Проверки ниже проходят по всем местам, где такое поле
есть, — чтобы новый блок с уникальным полем не завёл пятую копию этой ошибки.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from sqlalchemy import select

from core import exceptions as errors
from core import uniqueness
from tests.conftest import API


def _number_taken(db):
    """Проверка «номер занят» для вставок с повтором."""
    from database.models import Document

    return lambda row: db.scalar(
        select(Document.id).where(Document.number == row.number)
    ) is not None


def test_a_taken_value_is_reported_not_crashed(root_client):
    """`insert_unique`: занято — говорим, что занято."""
    from database.models import Role
    from database.session import SessionLocal

    with SessionLocal() as db:
        taken = lambda row: db.scalar(
            select(Role.id).where(Role.name == row.name)
        ) is not None
        first = uniqueness.insert_unique(
            db, Role(name="Проба уникальности"), taken=taken, message="занято", code="probe_taken"
        )
        db.commit()
        role_id = first.id

        try:
            with pytest.raises(errors.ConflictError) as refused:
                uniqueness.insert_unique(
                    db,
                    Role(name="Проба уникальности"),
                    taken=taken,
                    message="занято",
                    code="probe_taken",
                )
            assert refused.value.code == "probe_taken"

            # Сессия осталась пригодной: после отказа с ней можно работать
            # дальше. Ради этого вставка и идёт под точкой отката.
            assert db.get(Role, role_id) is not None
        finally:
            db.delete(db.get(Role, role_id))
            db.commit()


def test_retrying_takes_the_next_free_value():
    """`insert_retrying`: значение считается заново перед каждой попыткой."""
    from database.models import Document
    from database.session import SessionLocal

    with SessionLocal() as db:
        taken = "9999-000001"
        db.add(Document(number=taken, kind="intake", locale="ru", status="issued", payload="{}"))
        db.flush()

        counter = {"n": 0}

        def build():
            counter["n"] += 1
            # Первая попытка метит в занятый номер, вторая — в свободный.
            number = taken if counter["n"] == 1 else "9999-000002"
            return Document(number=number, kind="intake", locale="ru", status="issued", payload="{}")

        placed = uniqueness.insert_retrying(
            db, build, taken=_number_taken(db), message="нет места", code="probe_full"
        )
        assert counter["n"] == 2, "значение не пересчитали перед второй попыткой"
        assert placed.number == "9999-000002"
        db.rollback()


def test_retrying_gives_up_instead_of_spinning():
    """Не даётся трижды — отказ, а не бесконечный цикл."""
    from database.models import Document
    from database.session import SessionLocal

    with SessionLocal() as db:
        db.add(Document(number="9998-000001", kind="intake", locale="ru", status="issued", payload="{}"))
        db.flush()

        def always_taken():
            return Document(
                number="9998-000001", kind="intake", locale="ru", status="issued", payload="{}"
            )

        with pytest.raises(errors.ConflictError) as refused:
            uniqueness.insert_retrying(
                db, always_taken, taken=_number_taken(db), message="нет места", code="probe_full"
            )
        assert refused.value.code == "probe_full"
        db.rollback()


def test_a_different_broken_rule_is_not_disguised_as_a_conflict():
    """Нарушено не то ограничение — ошибка не прячется под «занято».

    Иначе первая же опечатка в модели (потерянный NOT NULL, чужой внешний ключ)
    вернулась бы человеку как «такое имя уже есть», и искать её начали бы не
    там. `insert_unique` честно превращает в конфликт ЛЮБОЕ нарушение — значит
    звать его можно только там, где уникальность единственное ограничение,
    которое вообще может нарушиться; здесь это и проверяется на живом примере.
    """
    from database.models import StockMove
    from database.session import SessionLocal

    with SessionLocal() as db:
        # Движение без товара: нарушается внешний ключ, а не уникальность.
        # «Занятого» после отказа не находится — значит ошибка чужая и обязана
        # уйти наверх как есть, а не превратиться в «такое имя уже есть».
        with pytest.raises(IntegrityError):
            uniqueness.insert_unique(
                db,
                StockMove(product_id=10**9, quantity_milli=1000, kind="in"),
                taken=lambda row: False,
                message="занято",
                code="probe_taken",
            )
        db.rollback()


def test_two_products_cannot_take_one_sku(root_client):
    """Артикул: приёмка товара — как раз то место, где двое работают разом."""
    root_client.post(f"{API}/modules/warehouse", json={"enabled": True})
    first = root_client.post(
        f"{API}/warehouse/products", json={"name": "Матрица A", "sku": "SKU-RACE-1"}
    )
    assert first.status_code == 201, first.text

    second = root_client.post(
        f"{API}/warehouse/products", json={"name": "Матрица B", "sku": "SKU-RACE-1"}
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "sku_taken"

"""telephony: журнал звонков и нормализованные номера клиентов

Revision ID: a9e64c17f235
Revises: b81f6a2d40c7
Create Date: 2026-08-05 12:00:00.000000

Два изменения одной миграцией, потому что порознь они бессмысленны:

* ``phone_calls`` — сами звонки;
* ``clients.phone_norm`` — по нему звонок находит клиента. Без этой колонки
  поиск промахивается на каждом варианте записи номера (``+380 67…`` против
  ``067…``), и телефония «не работает» через раз.

Направление записи в ленте (``client_notes.direction``) уже есть — его завела
миграция ленты (b81f6a2d40c7), на которую эта опирается.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9e64c17f235'
down_revision: Union[str, None] = "d6b30f84c917"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'phone_calls',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(length=128), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('from_number', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('from_number_norm', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('to_number', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('to_number_norm', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        # nullable: «длительность неизвестна» — не ноль секунд
        sa.Column('duration_sec', sa.Integer(), nullable=True),
        # nullable: пусто — звонок ещё не завершён
        sa.Column('outcome', sa.String(length=16), nullable=True),
        sa.Column('recording_path', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('deal_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('note_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # Удалили клиента, заявку, сотрудника или запись ленты — звонок остаётся:
        # это состоявшийся факт, и молча исчезать вместе с карточкой он не должен.
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['note_id'], ['client_notes.id'], ondelete='SET NULL'),
    )

    with op.batch_alter_table('phone_calls', schema=None) as batch_op:
        # уникальность внешнего идентификатора — на ней держится идемпотентность
        # приёма событий: повтор события не может создать второй звонок
        batch_op.create_index(
            batch_op.f('ix_phone_calls_external_id'), ['external_id'], unique=True
        )
        batch_op.create_index(
            batch_op.f('ix_phone_calls_from_number_norm'), ['from_number_norm'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_phone_calls_to_number_norm'), ['to_number_norm'], unique=False
        )
        batch_op.create_index(batch_op.f('ix_phone_calls_started_at'), ['started_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_phone_calls_outcome'), ['outcome'], unique=False)
        batch_op.create_index(batch_op.f('ix_phone_calls_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_phone_calls_deal_id'), ['deal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_phone_calls_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_phone_calls_note_id'), ['note_id'], unique=False)

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('phone_norm', sa.String(length=32), nullable=False, server_default='')
        )
        batch_op.create_index(batch_op.f('ix_clients_phone_norm'), ['phone_norm'], unique=False)

    _backfill_client_phone_norm()


def _backfill_client_phone_norm() -> None:
    """Заполняет phone_norm у существующих карточек.

    Без этого звонки не находили бы никого из тех, кто заведён до обновления, —
    а это и есть вся база. Код страны берём из настроек: тот же, что применяется
    при сохранении карточки, иначе половина базы нормализовалась бы по одному
    правилу, половина по другому.
    """
    from core.utils import normalize_phone

    bind = op.get_bind()
    # `key` — зарезервированное слово в MySQL, и голым текстом этот запрос там
    # падает на разборе (ERROR 1064). Собираем через конструкции SQLAlchemy: она
    # закавычит имя так, как принято в текущем движке.
    site_settings = sa.table(
        "site_settings", sa.column("key", sa.String), sa.column("value", sa.String)
    )
    code = bind.execute(
        sa.select(site_settings.c.value).where(
            site_settings.c.key == "default_country_code"
        )
    ).scalar() or ''
    # Засев ПАЧКАМИ, а не по строке. Правило проекта (CLAUDE.md §1, шаг 4)
    # требует заполнять одним UPDATE, и соседняя миграция `d3b8c05f1e2a` сама
    # называет цену обхода: «200 000 клиентов и 400 000 заявок — это минуты
    # вместо секунд».
    #
    # Одним запросом здесь всё же нельзя: `normalize_phone` — это питон
    # (плюс, ведущие нули, код страны, добавочный), и переписать его выражением
    # SQL значило бы завести ВТОРУЮ нормализацию. Разошлись бы они молча, и
    # заявка просто перестала бы находить своего клиента.
    #
    # Отсюда пачки: значения считаются питоном, а уезжают одним `UPDATE ... CASE`
    # на тысячу строк. Тысяча — размер, при котором запрос ещё далеко от
    # `max_allowed_packet` (по умолчанию 64 МБ у MySQL 8), а число обращений
    # падает в тысячу раз: 200 000 строк это 200 запросов вместо 200 000.
    #
    # Цена промедления не в терпении: точка входа гонит миграции ДО старта
    # приложения, `/healthz` всё это время не отвечает, а автообновление ждёт
    # его около двух минут и потом откатывает и код, и базу.
    RAZMER_PACHKI = 1000
    rows = bind.execute(sa.text("SELECT id, phone FROM clients WHERE phone <> ''")).fetchall()
    pachka = []

    def _slit(pachka):
        if not pachka:
            return
        # `CASE id WHEN … THEN …` одним запросом на всю пачку. Имена параметров
        # свои у каждой пары, потому что значения разные.
        vetki = " ".join(f"WHEN :id{n} THEN :norm{n}" for n in range(len(pachka)))
        gde = ", ".join(f":id{n}" for n in range(len(pachka)))
        dovody = {}
        for n, (client_id, normalized) in enumerate(pachka):
            dovody[f"id{n}"] = client_id
            dovody[f"norm{n}"] = normalized
        bind.execute(
            sa.text(
                f"UPDATE clients SET phone_norm = CASE id {vetki} END WHERE id IN ({gde})"
            ),
            dovody,
        )

    for client_id, phone in rows:
        normalized = normalize_phone(phone or '', code)[:32]
        if not normalized:
            continue
        pachka.append((client_id, normalized))
        if len(pachka) >= RAZMER_PACHKI:
            _slit(pachka)
            pachka = []
    _slit(pachka)


def downgrade() -> None:
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_clients_phone_norm'))
        batch_op.drop_column('phone_norm')

    # Индексы журнала отдельно не снимаем — их уносит `drop_table`, а на MySQL
    # снятие индекса под живым внешним ключом отвергается (1553). Разбор — в
    # откате `e4451c527c34`. Здесь на это упирался `ix_phone_calls_note_id`.
    op.drop_table('phone_calls')

    # Записи ленты, созданные звонками, остаются: это часть истории общения с
    # клиентом, и принадлежит она клиенту, а не блоку телефонии. Настройки
    # (строки telephony_* в site_settings) тоже остаются — это данные, а не
    # схема: откат делают, чтобы починить что-то другое и накатить обратно, и
    # вписанный в АТС секрет должен пережить такую поездку.

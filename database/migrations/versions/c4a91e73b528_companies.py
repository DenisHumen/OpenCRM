"""companies

Revision ID: c4a91e73b528
Revises: b81f6a2d40c7
Create Date: 2026-08-06 10:00:00.000000

Свои юрлица: от чьего имени работаем.

У малого бизнеса юрлиц обычно больше одного — ФОП для розницы и ООО для
договоров, — и счёт выставляется от одного, а акт подписывается другим.
Реквизиты хранить было негде, поэтому бланки печатались без них.

Ровно одна фирма может быть «по умолчанию». Частичного уникального индекса
(`UNIQUE ... WHERE is_default`) здесь нет намеренно: docs/03-database.md
запрещает диалект-специфичные фичи ради переезда на MySQL, где таких индексов
нет вовсе. Обычный UNIQUE не подходит тем более — он запретил бы двум фирмам
одновременно НЕ быть основной. Единственность держит сервис
(core/services/company_service.py), а миграция об этом только предупреждает.

`deals.company_id` необязателен и приходит с ON DELETE SET NULL: удаление фирмы
не должно ни уносить заявки за собой (CASCADE), ни запрещать саму чистку
справочника (RESTRICT). Существующие заявки переживают это без изменений —
пустая ссылка читается как «фирма по умолчанию».
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не `DEFAULT ''`:
# обычную форму MySQL отвергает (ошибка 1101), и миграция обрывается на середине.
# Скобки понимают оба движка. Подробности — database/types.text_default.
import sqlalchemy as sa


revision: str = 'c4a91e73b528'
down_revision: Union[str, None] = 'b81f6a2d40c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'companies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('legal_name', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('tax_number', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('reg_number', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('vat_number', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('legal_address', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('actual_address', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('phone', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('email', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('website', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('bank_name', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('bank_account', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('bank_code', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('signatory_name', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('signatory_basis', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('signature_path', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('stamp_path', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('note', sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_companies_name'), ['name'], unique=False)
        # По этим двум колонкам фильтруются оба списка, которые вообще бывают:
        # «все живые фирмы» и «какая основная».
        batch_op.create_index(batch_op.f('ix_companies_is_default'), ['is_default'], unique=False)
        batch_op.create_index(batch_op.f('ix_companies_deleted_at'), ['deleted_at'], unique=False)

    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_deals_company_id'), ['company_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_deals_company_id', 'companies', ['company_id'], ['id'], ondelete='SET NULL'
        )


def downgrade() -> None:
    with op.batch_alter_table('deals', schema=None) as batch_op:
        batch_op.drop_constraint('fk_deals_company_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_deals_company_id'))
        batch_op.drop_column('company_id')

    with op.batch_alter_table('companies', schema=None) as batch_op:
        for name in ('ix_companies_deleted_at', 'ix_companies_is_default', 'ix_companies_name'):
            batch_op.drop_index(batch_op.f(name))
    op.drop_table('companies')

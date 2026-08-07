"""documents

Revision ID: c9e14b83f27a
Revises: b7d94f2ae610
Create Date: 2026-08-05 14:00:00.000000

Бланк приёма вещи в работу: половина клиенту, половина остаётся в мастерской.

Хранится не картинкой, а данными — снимком напечатанного, номером и состоянием.
Так бланк можно перепечатать на другом языке, найти сканом штрихкода и провести
по состояниям. Снимок именно снимок: у человека на руках бумага, и она обязана
совпадать с базой, даже если клиента потом переименовали, а сделку удалили.
"""
from typing import Sequence, Union

from alembic import op
# У TEXT значение по умолчанию записано выражением — `DEFAULT ('')`, не `DEFAULT ''`:
# обычную форму MySQL отвергает (ошибка 1101), и миграция обрывается на середине.
# Скобки понимают оба движка. Подробности — database/types.text_default.
import sqlalchemy as sa


revision: str = 'c9e14b83f27a'
down_revision: Union[str, None] = 'b7d94f2ae610'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(length=24), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False, server_default='intake'),
        sa.Column('locale', sa.String(length=2), nullable=False, server_default='ru'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='issued'),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('deal_id', sa.Integer(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['deal_id'], ['deals.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documents_number'), ['number'], unique=True)
        batch_op.create_index(batch_op.f('ix_documents_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_deal_id'), ['deal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_created_at'), ['created_at'], unique=False)

    op.create_table(
        'document_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('from_status', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('to_status', sa.String(length=16), nullable=False),
        sa.Column('note', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('author_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('document_events', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_document_events_document_id'), ['document_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_document_events_created_at'), ['created_at'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('document_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_events_created_at'))
        batch_op.drop_index(batch_op.f('ix_document_events_document_id'))
    op.drop_table('document_events')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        for name in (
            'ix_documents_created_at', 'ix_documents_deal_id', 'ix_documents_client_id',
            'ix_documents_status', 'ix_documents_number',
        ):
            batch_op.drop_index(batch_op.f(name))
    op.drop_table('documents')

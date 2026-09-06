"""Срок заказа: `documents.due_at`.

Колонка nullable и с индексом: населённые строки заполнять нечем — срока у
старых заказов не называли, и пустота здесь честнее любой подстановки.
`downgrade` снимает индекс и колонку; заказы остаются, только без срока.

Revision ID: e5f2b7c1d9a4
Revises: d4e7a1c9b2f6
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f2b7c1d9a4"
down_revision: Union[str, None] = "d4e7a1c9b2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("due_at", sa.DateTime(), nullable=True))
    op.create_index("ix_documents_due_at", "documents", ["due_at"])


def downgrade() -> None:
    op.drop_index("ix_documents_due_at", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("due_at")

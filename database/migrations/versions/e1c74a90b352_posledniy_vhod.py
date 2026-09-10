"""Последний вход сотрудника: `users.last_login_at`.

Колонка понадобилась редизайну экрана «Сотрудники»: в таблице есть колонка
«Посл. вход», и взять её было неоткуда. Присутствие (`last_seen_at`) на этот
вопрос не отвечает — оно обновляется на любую активность и переживает выход, а
строки `user_sessions` сносятся при выходе, сбросе пароля и отключении, то есть
историю входов восстанавливать не из чего.

Nullable без умолчания: у существующих сотрудников входа в этой колонке нет и
взяться ему неоткуда — придумывать «время создания» значило бы соврать. Пусто
показывается как «ни разу», и это правда.

Revision ID: e1c74a90b352
Revises: d8b3c05e71a4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1c74a90b352"
down_revision: Union[str, None] = "d8b3c05e71a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_login_at")

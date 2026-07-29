"""work project url

Revision ID: c3d81a5e9f42
Revises: b7f2a9c14d3e
Create Date: 2026-07-28 12:00:00.000000

Ссылка на проект клиента у работы: каждая картинка на витрине может быть
отдельным кейсом. Пустая строка = кнопки перехода нет.

Настройка studio_site_url («вернуться на сайт студии») отдельной миграции не
требует — site_settings это таблица ключ-значение, недостающие ключи
досеиваются на старте (settings_service.seed_defaults).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d81a5e9f42'
down_revision: Union[str, None] = 'b7f2a9c14d3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('works', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('project_url', sa.String(length=500), nullable=False, server_default='')
        )


def downgrade() -> None:
    with op.batch_alter_table('works', schema=None) as batch_op:
        batch_op.drop_column('project_url')

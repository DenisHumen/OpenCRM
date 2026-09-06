"""Блок «Глобус»: точка клиента, пояс гостя, тумблер доски.

Три колонки, все с умолчанием или nullable: заполнять существующие строки
нечем и не нужно — место клиента считается по стране, а пояса у прошлых
просмотров нет и взяться ему неоткуда. `downgrade` снимает колонки; данные
клиентов, просмотров и досок при этом целы.

Revision ID: a4d9c6e2f107
Revises: f2a7c9e4b1d6
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4d9c6e2f107"
down_revision: Union[str, None] = "f2a7c9e4b1d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("lat_e7", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("lon_e7", sa.Integer(), nullable=True))
    with op.batch_alter_table("share_views") as batch:
        batch.add_column(sa.Column("tz", sa.String(length=64), nullable=False, server_default=""))
    with op.batch_alter_table("boards") as batch:
        batch.add_column(sa.Column("geo_enabled", sa.Boolean(), nullable=False, server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("boards") as batch:
        batch.drop_column("geo_enabled")
    with op.batch_alter_table("share_views") as batch:
        batch.drop_column("tz")
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("lon_e7")
        batch.drop_column("lat_e7")

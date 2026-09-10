"""Снят блок «Глобус»: тумблер доски и часовой пояс гостя.

Владелец 09.09.2026: блок бесполезен и только нагромождает систему. Вместе с
планетой уходят две колонки, заведённые РАДИ неё:

* `boards.geo_enabled` — собирать ли пояс гостя у этой доски;
* `share_views.tz` — сам пояс, по которому гость ставился точкой на планету.

**`clients.lat_e7` и `clients.lon_e7` НЕ трогаются.** Их завела та же ревизия
`a4d9c6e2f107`, но на них стоит миниатюра карты в карточке клиента и подсказки
в поле адреса — они остаются. Соблазн «снять всё, что добавил глобус» ведёт
здесь прямо к потере координат, которые человек ставил руками.

Ту ревизию править нельзя (`CLAUDE.md` §1, `tests/test_migratsii_ne_pravyat.py`):
она выгружена, alembic применённое заново не гоняет, и правка не выполнится
никогда. Поэтому новая.

`downgrade` возвращает обе колонки с прежними умолчаниями — данных в них уже
нет, и взяться им неоткуда: собиравшего их кода в системе не осталось.

Revision ID: d8b3c05e71a4
Revises: c7f42b81e903
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8b3c05e71a4"
down_revision: Union[str, None] = "c7f42b81e903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("share_views") as batch:
        batch.drop_column("tz")
    with op.batch_alter_table("boards") as batch:
        batch.drop_column("geo_enabled")


def downgrade() -> None:
    with op.batch_alter_table("boards") as batch:
        batch.add_column(
            sa.Column("geo_enabled", sa.Boolean(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("share_views") as batch:
        batch.add_column(sa.Column("tz", sa.String(length=64), nullable=False, server_default=""))

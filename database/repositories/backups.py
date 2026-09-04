"""Заливка копии базы: файл дампа уходит на сервер целиком, одним разговором.

Резать дамп по `;` нельзя: точка с запятой живёт и внутри значений, и самодельный
резак проверял бы сам себя, а не копию, — ровно то место, где дампер и врёт.
Поэтому `CLIENT.MULTI_STATEMENTS` и один `execute` на весь текст, как
`mysql < ФАЙЛ` руками. Тот же приём доказан кругом «снял → залил → сверил» в
`tests/test_vosstanovlenie_kopii.py`.

Живёт в `database/`, а не в службе: это разговор с движком о том, что в него
кладут, и граница базы (`tests/test_db_boundary.py`) держит его здесь.
"""

from pathlib import Path

from pymysql.constants import CLIENT
from sqlalchemy import create_engine

#: Сколько ждать чужой замок на таблицу. Заливка начинается с `DROP TABLE`, а
#: тот ждёт метаданных у всякой открытой транзакции — в том числе у запроса,
#: который заливку и позвал, если он не зафиксировался. Без предела ручка
#: висела бы до `lock_wait_timeout` сервера, а это год.
ZHDAT_ZAMOK_SEKUND = 30


def zalit_damp(url: str, damp: Path) -> None:
    """Залить файл дампа в базу по адресу `url`. Дамп сам роняет и создаёт таблицы."""
    dvizhok = create_engine(url, connect_args={"client_flag": CLIENT.MULTI_STATEMENTS})
    syroe = dvizhok.raw_connection()
    try:
        kursor = syroe.cursor()
        kursor.execute(
            f"SET SESSION lock_wait_timeout={ZHDAT_ZAMOK_SEKUND};\n"
            + damp.read_text(encoding="utf-8")
        )
        while kursor.nextset():
            pass
        syroe.commit()
    finally:
        syroe.close()
        dvizhok.dispose()

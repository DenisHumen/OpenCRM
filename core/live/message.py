"""Намёк: что перечитать. Данных в нём нет — и не бывает.

Правило видимости (`docs/ustroystvo/12-zhivye-obnovleniya.md` §1–§2): в намёк кладётся только то,
чего не прячет ни одно право — тема, действие, номер записи и поле, по
которому идёт отбор (`manager_id` у заявки). Деньги, тексты, имена, состав —
никогда: их спрячет обработчик `GET`, через который клиент и перечитает.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: Закрытый список полей. Поле не из списка — отказ при разборе, а не молчание.
FIELDS = ("topic", "action", "id", "scope_key", "actor_id", "module")

ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_DELETED = "deleted"
ACTIONS = (ACTION_CREATED, ACTION_UPDATED, ACTION_DELETED)


@dataclass(frozen=True)
class Hint:
    topic: str
    action: str
    id: int | None = None
    #: Поле отбора: `manager_id` у заявки. Пусто — отбор по одному праву.
    scope_key: int | None = None
    #: Кто изменил. Показывается на экране (решение владельца, §14 п. 3);
    #: сотрудники видят друг друга списком `/people`, так что прятать нечего.
    actor_id: int | None = None
    module: str | None = None

    def __post_init__(self) -> None:
        if not self.topic or not isinstance(self.topic, str):
            raise ValueError("hint needs a topic")
        if self.action not in ACTIONS:
            raise ValueError(f"unknown hint action: {self.action!r}")

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Hint":
        dannye = json.loads(text)
        if not isinstance(dannye, dict):
            raise ValueError("hint must be an object")
        lishnie = set(dannye) - set(FIELDS)
        if lishnie:
            raise ValueError(f"hint carries fields it must not: {sorted(lishnie)}")
        return cls(**dannye)

    @property
    def key(self) -> tuple[str, int | None]:
        """По чему намёки склеиваются: одна тема и одна запись — один намёк."""
        return (self.topic, self.id)

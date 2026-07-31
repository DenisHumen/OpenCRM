"""Что развёрнуто сейчас и чем кончались прошлые попытки обновления.

Два файла, потому что у них разная природа: `state.json` перезаписывается и
описывает «сейчас», `history.jsonl` только дописывается и описывает «как мы
сюда пришли». Даже если состояние потеряется, по истории видно, что случилось.

Запись атомарная (временный файл → `os.replace`): обновление может оборваться в
любой момент — питание, `docker compose` уронил хост, — и недописанный JSON
оставил бы демон без состояния.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Journal:
    def __init__(self, state_file: Path, history_file: Path) -> None:
        self.state_file = Path(state_file)
        self.history_file = Path(history_file)

    # --- состояние ---

    def read(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, **changes) -> dict:
        state = self.read()
        state.update(changes)
        _atomic_write(self.state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        return state

    @property
    def autoupdate_enabled(self) -> bool:
        """По умолчанию — включено: демон ставят именно ради автообновления."""
        return bool(self.read().get("autoupdate", True))

    def set_autoupdate(self, enabled: bool) -> None:
        self.write(autoupdate=bool(enabled))

    @property
    def deployed_sha(self) -> str:
        return str(self.read().get("deployed_sha") or "")

    @property
    def etag(self) -> str:
        return str(self.read().get("etag") or "")

    # --- история ---

    def append(self, record: dict) -> dict:
        record = {"at": record.get("at") or time.time(), **record}
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def history(self, limit: int = 10) -> list[dict]:
        """Последние записи, новые первыми."""
        try:
            lines = self.history_file.read_text("utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # порченую строку пропускаем, остальную историю не теряем
            if len(records) >= limit:
                break
        return records

    def last(self) -> dict | None:
        records = self.history(1)
        return records[0] if records else None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise

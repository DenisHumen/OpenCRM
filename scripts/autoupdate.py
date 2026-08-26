"""Автообновление боевого сайта из ветки на GitHub (см. deploy/updater.py).

Запускается **на хосте**, не в контейнере: обновляющий пересобирает контейнеры и
внутри одного из них не пережил бы собственный деплой. Нужны только Python 3.12,
git и docker — зависимостей у пакета `deploy` нет принципиально.

    python scripts/autoupdate.py status          # версия, есть ли обновление, чем кончилось прошлое
    python scripts/autoupdate.py check           # один проход: появился коммит — обновить
    python scripts/autoupdate.py watch           # то же по кругу (systemd-юнит: deploy/systemd/)
    python scripts/autoupdate.py force-update    # передеплоить сейчас, не дожидаясь опроса
    python scripts/autoupdate.py enable|disable  # флаг автообновления
    python scripts/autoupdate.py history -n 20

Настройки — переменные окружения OPENCRM_UPDATE_* (deploy/autoupdate.env.example).
Код возврата 1 — обновление не поехало или откатилось; так `check` в cron даёт
знать о проблеме, даже если Telegram не настроен.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy.config import UpdateConfig  # noqa: E402
from deploy.github import chas  # noqa: E402
from deploy.journal import Journal  # noqa: E402
from deploy.updater import STATUS_UP_TO_DATE, Updater  # noqa: E402


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _short(sha: str) -> str:
    return sha[:12] if sha else "—"


def cmd_status(state: dict) -> int:
    """Печатает УЖЕ СОБРАННОЕ состояние.

    Раньше собирала сама, и `--cached` пришлось бы протаскивать сюда вторым
    путём. Два пути к одному запросу — это ровно то, из-за чего запрос однажды
    уходит в сеть там, где обещано не уходить.
    """
    print(f"репозиторий:    {state['repo']}@{state['branch']}")
    print(f"развёрнуто:     {_short(state['deployed'])}")
    print(f"в ветке:        {_short(state['available'])}" + (f"  ({state['github_error']})" if state["github_error"] else ""))
    # Ожидание лимита — первое, что надо увидеть: пока оно стоит, ни «в
    # ветке», ни «обновление» не обновятся, сколько ни спрашивай.
    if state.get("github_wait", 0) > time.time():
        print(
            "лимит GitHub:   исчерпан, опрос возобновится в "
            + chas(state["github_wait"])
        )
    if state.get("available_at"):
        print(f"проверено:      {time.strftime('%Y-%m-%d %H:%M', time.localtime(state['available_at']))}")
    # «Неизвестно», а не «нет». Разница не в вежливости: с `--cached` до первого
    # опроса голова ветки неизвестна, и «обновление: нет» означало бы «всё
    # свежее» — самый спокойный из возможных ответов там, где мы не спрашивали.
    if state["available"]:
        print(f"обновление:     {'есть' if state['update_available'] else 'нет'}")
    else:
        print("обновление:     неизвестно")
    if state.get("checks"):
        names = {"success": "зелёные", "pending": "идут", "failure": "красные"}
        print(f"проверки:       {names.get(state['checks'], state['checks'])} — {state['checks_detail']}")
    print(f"автообновление: {'включено' if state['autoupdate'] else 'выключено'}")
    if state["failed_sha"]:
        print(f"не встал:       {_short(state['failed_sha'])} (повтор — force-update)")
    last = state["last"]
    if last:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last.get("at", 0)))
        print(f"последнее:      {when} — {last.get('status')} {last.get('reason', '')}".rstrip())
    else:
        print("последнее:      обновлений ещё не было")
    return 0


def cmd_history(updater: Updater, limit: int) -> int:
    records = updater.journal.history(limit)
    if not records:
        print("история пуста")
        return 0
    for record in records:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("at", 0)))
        print(f"{when}  {record.get('status'):<12} {_short(record.get('to_sha', ''))}  "
              f"{record.get('summary', '')}".rstrip())
        if record.get("reason"):
            print(f"{'':18}└ {record['reason']}")
    return 0


def cmd_run(updater: Updater, force: bool) -> int:
    outcome = updater.run_once(force=force)
    if outcome.status == STATUS_UP_TO_DATE:
        log(f"обновлений нет ({_short(outcome.from_sha)})")
    else:
        log(f"{outcome.status}: {outcome.reason or _short(outcome.to_sha)}")
    for step in outcome.steps:
        log(f"  {'ok ' if step.ok else 'FAIL'} {step.name}" + (f" — {step.detail}" if step.detail else ""))
    return 0 if outcome.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        choices=["status", "check", "watch", "force-update", "enable", "disable", "history"],
        nargs="?",
        default="check",
    )
    parser.add_argument("-n", "--limit", type=int, default=10, help="сколько записей истории показать")
    parser.add_argument("--json", action="store_true", help="status/check машинным форматом")
    parser.add_argument(
        "--cached",
        action="store_true",
        help="status: не спрашивать GitHub, ответить запомненным с прошлого опроса",
    )
    args = parser.parse_args(argv)

    config = UpdateConfig.from_env()
    journal = Journal(config.state_file, config.history_file)

    if args.command in {"enable", "disable"}:
        journal.set_autoupdate(args.command == "enable")
        print(f"автообновление {'включено' if args.command == 'enable' else 'выключено'}")
        return 0

    updater = Updater(config, journal=journal, log=log)

    if args.command == "status":
        state = updater.status(fresh=not args.cached)
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        return cmd_status(state)
    if args.command == "history":
        return cmd_history(updater, args.limit)
    if args.command == "watch":
        log(f"слежу за {config.repo}@{config.branch}, опрос раз в {config.poll_seconds} c")
        updater.watch()
        return 0
    return cmd_run(updater, force=args.command == "force-update")


if __name__ == "__main__":
    sys.exit(main())

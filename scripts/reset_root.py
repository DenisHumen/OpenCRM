"""Управление root-аккаунтом: смена email и пароля, восстановление доступа.

Root создаётся один раз при первом старте. Если позже поменять OPENCRM_ROOT_EMAIL
в config/.env, ничего не произойдёт — аккаунт уже есть, и вход под новым адресом
вернёт 401. Этот скрипт приводит аккаунт в соответствие с настройками.

Примеры:
    # взять email и пароль из config/.env (OPENCRM_ROOT_EMAIL / OPENCRM_ROOT_PASSWORD)
    python scripts/reset_root.py --from-env

    # задать явно
    python scripts/reset_root.py --email me@studio.site --password "новый-пароль"

    # только показать, какой root сейчас в базе
    python scripts/reset_root.py --show

Требует доступа к файлу БД, то есть запускается на сервере. По умолчанию
выставляет флаг «сменить пароль при следующем входе» — снять можно --no-force-change.
"""

import argparse
import getpass
import sys
from pathlib import Path

# запуск вида `python scripts/reset_root.py` кладёт в sys.path каталог scripts/,
# поэтому корень проекта добавляем явно
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from core.security import passwords  # noqa: E402
from core.utils import is_valid_email, normalize_email, now_utc  # noqa: E402
from database.models import User  # noqa: E402
from database.models.user import ROLE_ROOT, STATUS_ACTIVE  # noqa: E402
from database.repositories import users as users_repo  # noqa: E402
from database.session import get_session  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление root-аккаунтом OpenCRM")
    parser.add_argument("--email", help="новый email для входа")
    parser.add_argument("--password", help="новый пароль (без флага — спросит скрыто)")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="взять пароль первой строкой стандартного ввода (не виден в ps)",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="взять email и пароль из OPENCRM_ROOT_EMAIL / OPENCRM_ROOT_PASSWORD",
    )
    parser.add_argument("--show", action="store_true", help="показать текущий root и выйти")
    parser.add_argument(
        "--no-force-change",
        action="store_true",
        help="не требовать смену пароля при следующем входе",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = get_session()
    try:
        root = users_repo.get_root(db)

        if args.show:
            if root is None:
                print("root-аккаунта в базе нет — он будет создан при следующем старте приложения.")
            else:
                print(f"email:  {root.email}")
                print(f"статус: {root.status}")
                print(f"смена пароля при входе: {'да' if root.must_change_password else 'нет'}")
            return 0

        if args.from_env:
            email = settings.root_email
            password = settings.root_password
        else:
            email = args.email or (root.email if root else None)
            password = args.password
            if args.password_stdin:
                # Со стандартного ввода, а НЕ аргументом. Аргументы видны в
                # `ps` любому пользователю машины (/proc/<pid>/cmdline читается
                # всеми) и оседают в `docker inspect`. Пароль владельца системы
                # — последнее, что стоит там оставлять.
                #
                # Правило в проекте не новое, просто сюда не дошло: так уже
                # заведено у пароля наблюдателя базы (`grant_db_exporter`) и у
                # пароля панели (`monitoring password`), и в обоих местах
                # рядом записано, почему.
                password = sys.stdin.readline().rstrip(chr(10)).rstrip(chr(13))
            elif not password:
                # `elif`, а не второй `if`, и это не стиль. С обычным `if`
                # пустой ввод проваливался сюда, и скрипт уходил спрашивать
                # пароль С ТЕРМИНАЛА — которого при обновлении нет, и он висел
                # бы вечно. Поймано собственной проверкой: она не покраснела, а
                # ЗАВИСЛА на две минуты.
                password = getpass.getpass("Новый пароль root: ")
                # не-секрет: сверяются два ввода ОДНОГО человека за одной
                # консолью. Узнать по времени он может лишь то, что сам набрал.
                if password != getpass.getpass("Повторите пароль: "):
                    print("Пароли не совпадают.", file=sys.stderr)
                    return 1

        if not email:
            print("Укажите --email (или --from-env).", file=sys.stderr)
            return 1
        email = normalize_email(email)
        if not is_valid_email(email):
            print(f"Некорректный email: {email}", file=sys.stderr)
            return 1
        if not password:
            print("Пустой пароль недопустим.", file=sys.stderr)
            return 1
        if not passwords.is_valid_password(password):
            print(
                f"Пароль короче {passwords.MIN_PASSWORD_LENGTH} символов — "
                "система не даст его подтвердить при смене.",
                file=sys.stderr,
            )
            return 1

        # чужой аккаунт с таким email заблокировал бы вход из-за уникального индекса
        clash = users_repo.get_by_email(db, email)
        if clash is not None and (root is None or clash.id != root.id):
            print(
                f"Email {email} уже занят аккаунтом #{clash.id} ({clash.role}). "
                "Удалите или переименуйте его в разделе «Сотрудники».",
                file=sys.stderr,
            )
            return 1

        if root is None:
            root = User(
                email=email,
                name="Root",
                password_hash=passwords.hash_password(password),
                role=ROLE_ROOT,
                status=STATUS_ACTIVE,
                approved_at=now_utc(),
            )
            db.add(root)
            action = "создан"
        else:
            root.email = email
            root.password_hash = passwords.hash_password(password)
            root.status = STATUS_ACTIVE
            action = "обновлён"

        root.must_change_password = not args.no_force_change
        db.flush()
        # все прежние сессии больше не действительны
        users_repo.delete_sessions_for_user(db, root.id)
        db.commit()

        print(f"root-аккаунт {action}: {email}")
        if root.must_change_password:
            print("При первом входе система потребует задать новый пароль.")
        print("Активные сессии root завершены — войдите заново.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

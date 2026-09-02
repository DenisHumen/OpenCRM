"""У каждого права в матрице есть спрашивающий.

**Зачем сторож.** Область, не названная в `AREA_ACTIONS`, получает набор по
умолчанию — четыре действия. Правило удобное («появился блок, появилась
строка»), но молчаливое: блок, у которого работают два действия из четырёх,
приносит два права, которые **выдать можно, а делают они ничего**. Управляющий
ставит галочку «править письма», сотрудник не получает ничего, и разобраться в
этом можно только чтением исходников.

Сам код это уже знает и говорит про `audit`: «право, которое ничего не даёт,
хуже его отсутствия». Держалось правило вниманием — и не удержалось: сплошной
перебор 02.09.2026 нашёл девять пустых прав из 82.

**Считается СПРОС, а не упоминание.** Первая редакция этой проверки искала код
права где угодно в исходниках — и зеленела на подрыве: строка `finance.delete`
нашлась в комментарии `core/permissions.py`, объясняющем, почему такого права
нет. Довод в комментарии засчитывался за работу. Поэтому здесь разбираются
именно формы вопроса, а комментарий вопросом не является.

Проверка не судит, ВЕРНО ли право спрошено, — только что его кто-то спрашивает.
"""

import pathlib
import re

from core import permissions

KOREN = pathlib.Path(__file__).resolve().parent.parent

#: Где право может спрашиваться. Фронт тоже: половина прав видна только там —
#: кнопкой, которая появляется или нет.
DEREVYA = ("core", "web", "database", "deploy", "scripts")
RASSHIRENIYA = (".py", ".ts", ".tsx")
MIMO = ("node_modules", "dist", "__pycache__")

#: Пустые права, оставленные нарочно, — с доводом.
#:
#: **Список пуст, и это состояние, а не заготовка.** Девять пустых прав,
#: найденных сплошным перебором 02.09.2026, убраны из матрицы в тот же день:
#: удаление бумаг (бланк, заказ, накладная — бумага отменяется, а не удаляется),
#: правка и удаление письма (почта — зеркало сервера), удаление звонка (журнал
#: только дописывается), заведение, правка и удаление в наблюдении (раздел
#: только показывает).
#:
#: Миграции это не потребовало, вопреки опасению плана. `codes_of_role`
#: сверяется с реестром (`permissions.exists`), поэтому осиротевшая строка
#: `role_permissions` не даёт ничего и наружу не видна; а первое же сохранение
#: должности переписывает набор целиком (`roles_repo.set_permissions`) и уносит
#: её само.
#:
#: Список закрытый: новое пустое право сюда не дописывается «чтобы позеленело».
PUSTYE_NAROCHNO: dict[str, str] = {}


#: Имя константы действия → её значение: `VIEW_OTHERS` → `view_others`.
#: Спрашивают и так: `has(db, user, "deals", permissions.VIEW_OTHERS)`.
KONSTANTY: dict[str, str] = {
    imya: znachenie
    for imya, znachenie in vars(permissions).items()
    if imya.isupper() and isinstance(znachenie, str)
}

#: `require_perm("mail", "view")`, `has(db, user, "deals", permissions.VIEW)`.
PARA = re.compile(r'"([a-z_]+)"\s*,\s*(?:"([a-z_]+)"|(?:permissions\.)?([A-Z][A-Z_]+))')
#: Фронт: `can(user, "clients.edit")`.
FRONT = re.compile(r'can\(\s*\w+\s*,\s*"([a-z_]+\.[a-z_]+)"')
#: `sees_amounts(db, user, "orders")` — код собирается на лету.
SUMMY = re.compile(r'sees_amounts\([^)]*?"(\w+)"')


def _ves_kod() -> str:
    kuski = []
    for koren in DEREVYA:
        for fayl in sorted((KOREN / koren).rglob("*")):
            if fayl.suffix not in RASSHIRENIYA:
                continue
            if any(chast in str(fayl) for chast in MIMO):
                continue
            kuski.append(fayl.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(kuski)


def sprosheny(kod: str) -> set[str]:
    """Права, которые кто-то действительно спрашивает."""
    najdeno: set[str] = set()
    for oblast, strokoy, konstantoy in PARA.findall(kod):
        deystvie = strokoy or KONSTANTY.get(konstantoy or "", "")
        if deystvie:
            najdeno.add(f"{oblast}.{deystvie}")
    najdeno |= set(FRONT.findall(kod))
    najdeno |= {f"{oblast}.{permissions.VIEW_AMOUNTS}" for oblast in SUMMY.findall(kod)}
    if re.search(r"sees_amounts\(db, user\)", kod):
        najdeno.add(f"deals.{permissions.VIEW_AMOUNTS}")  # значение по умолчанию
    return najdeno


def vse_kody() -> list[str]:
    return sorted(
        permissions.code(area.key, deystvie)
        for area in permissions.AREAS
        for deystvie in area.actions
    )


def test_perebor_prav_ne_pustoy():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    assert len(vse_kody()) > 50, "реестр прав не собрался"


def test_razbor_voprosov_ne_pustoy():
    """И сторож, не узнавший ни одной формы вопроса, — тоже."""
    najdeno = sprosheny(_ves_kod())
    assert len(najdeno) > 40, (
        f"форм вопроса разобрано {len(najdeno)} — сменился способ спрашивать права, "
        "и проверка ниже объявила бы мёртвой всю матрицу"
    )


def test_u_kazhdogo_prava_est_sprashivayushchiy():
    najdeno = sprosheny(_ves_kod())
    pustye = [polnyy for polnyy in vse_kody() if polnyy not in najdeno]

    lishnie = sorted(set(pustye) - set(PUSTYE_NAROCHNO))
    assert not lishnie, (
        "право выдать можно, а делает оно ничего: "
        + ", ".join(lishnie)
        + ". Либо за ним заводится работа, либо действие уходит из "
        "`AREA_ACTIONS` (это правит редактор должностей и уже выданные роли — "
        "нужна миграция), либо оно называется в `PUSTYE_NAROCHNO` с доводом"
    )

    ozhili = sorted(set(PUSTYE_NAROCHNO) - set(pustye))
    assert not ozhili, (
        "право перестало быть пустым, а список исключений об этом не знает: "
        + ", ".join(ozhili)
        + ". Убрать из `PUSTYE_NAROCHNO`: список, в котором есть лишнее, "
        "перестают читать целиком"
    )

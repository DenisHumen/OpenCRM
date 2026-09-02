"""Секреты сравниваются за постоянное время, а не обычным `==`.

**Почему это не придирка.** Обычное сравнение строк обрывается на первом
несовпавшем знаке, и время ответа зависит от того, сколько знаков совпало.
Подбирающий узнаёт секрет ПО БУКВЕ: не за 256 бит перебора, а за длину строки
попыток. Ограничители в проекте от этого не спасают — они стоят против ПОТОКА, а
подбор в их пределах выглядит обычной работой.

В проекте таких мест пять: ключ приёма заявок, подпись вебхука АТС, вебхук
телеграма, приём тревог и метка подлинности в `secretbox`. Все пятеро сегодня
сравнивают правильно, и ни одного из них не стерёг никто: **подрыв
`compare_digest` → `==` не роняет ни одной проверки**, потому что поведение при
этом не меняется. Меняются наносекунды, а их в наборе тестов честно не измерить.

Отсюда проверка по исходникам.

**Разбором дерева, а не поиском по строке, и это выстрадано.** Первая редакция
искала секретные слова подстрокой и дала восемь ложных находок из девяти: `pin`
нашёлся в слове `ping`, `parol` — в имени константы `SPOSOB_PAROL`, а сравнение
длины (`len(klyuch) != 32`) секретом не является вовсе. Сторож, который на девять
криков ошибается восемь раз, размечают отметками до полной немоты.

Поэтому: сравнение считается подозрительным, только если ХОТЯ БЫ ОДНА сторона
названа секретом, а ДРУГАЯ не является ни литералом, ни `None`, ни константой в
верхнем регистре. Сравнение с литералом — это разбор команды или длины, а не
секрет.
"""

import ast
import pathlib

KOREN = pathlib.Path(__file__).resolve().parent.parent
KATALOGI = ("core", "web", "scripts", "deploy")

#: Слова, по которым видно, что значение держит секрет.
#:
#: `hash` и `otpechatok` здесь не случайно: отпечаток сравнивают ровно так же, и
#: утечка по времени у него та же — он выводится из секрета.
SEKRETNYE = (
    "secret", "sekret", "token", "klyuch", "podpis", "signature",
    "otpechatok", "hash", "pin", "parol", "password", "apikey",
    # `tag` и `mac` — метка подлинности в `secretbox`. Без них подрыв там не
    # краснел: метка секретом называется, а слова «секрет» в имени не носит.
    "tag", "mac", "metka", "digest", "hmac",
    # Голого `key` тут НЕТ намеренно: в этом коде так зовутся ключ этапа воронки
    # и ключ настройки, и он давал четыре ложные находки подряд. Секретные ключи
    # ловятся своими словами — `apikey`, `klyuch`, `secret`, `otpechatok`.
)

#: Отметка «здесь обычное сравнение законно» — рядом с местом, с доводом.
#:
#: Списком отдельным файлом это не делается по той же причине, что у фоновых
#: таймеров: ключом пришлось бы делать путь со строкой, а строки едут от любой
#: правки выше. Отметка живёт рядом и едет вместе с местом.
OTMETKA = "не-секрет:"


def _fayly():
    for katalog in KATALOGI:
        for put in sorted((KOREN / katalog).rglob("*.py")):
            if "__pycache__" in put.parts:
                continue
            yield put


def _imya(uzel) -> str:
    """Как названа сторона сравнения: `x`, `a.b`, `f(...)` → имя вызываемого."""
    if isinstance(uzel, ast.Name):
        return uzel.id
    if isinstance(uzel, ast.Attribute):
        return uzel.attr
    if isinstance(uzel, ast.Call):
        return _imya(uzel.func)
    if isinstance(uzel, ast.Subscript):
        return _imya(uzel.value)
    return ""


def _chasti(imya: str) -> set[str]:
    """Имя, разобранное на части: `pin_hash` → {pin, hash}, `_tagSum` → {tag, sum}.

    По ЧАСТЯМ, а не вхождением подстроки. Иначе `tag` находится внутри `stage`, и
    сторож начинает кричать на смену этапа заявки — три ложные находки подряд.
    """
    slovo = ""
    chasti = set()
    for znak in imya:
        if znak == "_" or znak.isupper():
            if slovo:
                chasti.add(slovo.lower())
            slovo = znak.lower() if znak.isupper() else ""
        else:
            slovo += znak
    if slovo:
        chasti.add(slovo.lower())
    return chasti


def _sekretnoe(uzel) -> bool:
    return bool(_chasti(_imya(uzel)) & set(SEKRETNYE))


def _tolko_chitaetsya(uzel) -> bool:
    """Сторона, о которой нечего узнавать по времени.

    Литерал (строка, число, `None`) и константа в верхнем регистре — это разбор
    команды, проверка длины или сверка с известным значением, а не секрет.
    """
    if isinstance(uzel, ast.Constant):
        return True
    if isinstance(uzel, ast.Name) and uzel.id.isupper():
        return True
    if isinstance(uzel, ast.Attribute) and uzel.attr.isupper():
        return True
    return False


def _dovod_ryadom(stroki: list[str], nomer: int) -> str:
    """Строка сравнения и комментарий НАД ней целиком, сколько бы он ни занял.

    Окном в три строки это не делается: довод бывает длиннее, и первая редакция
    проверки не увидела отметку, стоявшую четвёртой строкой выше. Сторож, чей
    разбор зависит от длины чужого объяснения, однажды покраснеет на правильном
    коде — а покрасневшему по пустяку перестают верить.
    """
    kuski = [stroki[nomer - 1]]
    i = nomer - 2
    while i >= 0 and stroki[i].lstrip().startswith("#"):
        kuski.append(stroki[i])
        i -= 1
    return "".join(kuski)


def _podozritelnye():
    for put in _fayly():
        stroki = put.read_text(encoding="utf-8").splitlines()
        derevo = ast.parse("\n".join(stroki))
        for uzel in ast.walk(derevo):
            if not isinstance(uzel, ast.Compare):
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in uzel.ops):
                continue
            storony = [uzel.left, *uzel.comparators]
            if not any(_sekretnoe(s) for s in storony):
                continue
            if any(_tolko_chitaetsya(s) for s in storony):
                continue
            nomer = uzel.lineno
            if OTMETKA in _dovod_ryadom(stroki, nomer):
                continue
            yield (
                f"{put.relative_to(KOREN).as_posix()}:{nomer}",
                stroki[nomer - 1].strip(),
            )


def test_perebor_smotrit_v_ishodniki():
    """Сторож, читающий пустоту, зеленеет на любой беде."""
    fayly = list(_fayly())
    assert len(fayly) > 100, (
        f"в переборе {len(fayly)} файлов — каталоги названы неверно, и проверка "
        "ниже стерегла бы пустоту"
    )
    kod = "\n".join(p.read_text(encoding="utf-8") for p in fayly)
    assert kod.count("compare_digest") >= 5, (
        "в проекте не нашлось привычных сравнений за постоянное время — сменился "
        "способ их писать, и проверка ниже ищет не то"
    )


def test_razbor_vidit_podlozhennoe(tmp_path):
    """Проверка самой проверки: подложенное сравнение обязано находиться.

    Разбор с отсевом литералов и констант легко сделать слишком щедрым — и он
    станет зелёным навсегда. Поэтому рядом стоит образец беды.
    """
    obrazets = tmp_path / "obrazets.py"
    obrazets.write_text(
        "def proverit(prislannyy, secret_key):\n"
        "    return prislannyy == secret_key\n",
        encoding="utf-8",
    )
    derevo = ast.parse(obrazets.read_text(encoding="utf-8"))
    nayden = [
        uzel
        for uzel in ast.walk(derevo)
        if isinstance(uzel, ast.Compare)
        and any(isinstance(op, ast.Eq) for op in uzel.ops)
        and any(_sekretnoe(s) for s in [uzel.left, *uzel.comparators])
        and not any(_tolko_chitaetsya(s) for s in [uzel.left, *uzel.comparators])
    ]
    assert nayden, "разбор не увидел подложенного сравнения секрета обычным `==`"


def test_sekret_ne_sravnivaetsya_obychnym_operatorom():
    nayden = list(_podozritelnye())
    assert not nayden, (
        "секрет сравнивается обычным оператором — он обрывается на первом "
        "несовпавшем знаке, и секрет подбирается ПО БУКВЕ по времени ответа:\n  "
        + "\n  ".join(f"{gde}: {stroka}" for gde, stroka in nayden)
        + "\nЗовите `hmac.compare_digest`. Если сравнивается не секрет — "
        "поставьте рядом отметку «не-секрет:» с доводом."
    )


def test_u_otmetki_est_dovod():
    """Отметка без довода — «потому что» без «почему».

    Голое слово отключает проверку и ничего не объясняет; через полгода его
    скопируют в следующее место, не думая.
    """
    pustye = []
    for put in _fayly():
        for nomer, stroka in enumerate(put.read_text(encoding="utf-8").splitlines(), 1):
            if OTMETKA not in stroka:
                continue
            if len(stroka.split(OTMETKA, 1)[1].strip()) < 10:
                pustye.append(f"{put.relative_to(KOREN).as_posix()}:{nomer}")
    assert not pustye, (
        "у отметки «не-секрет» нет довода: " + ", ".join(pustye)
        + ". Напишите, почему это сравнение секретом не является"
    )

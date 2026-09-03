"""Повторяющийся запрос обязан спрашивать, видна ли вкладка.

**Беда, которая была в продукте, и не одна.** Свёрнутая вкладка продолжала
ходить на сервер: проверка свободного места — пять запросов раз в две минуты с
КАЖДОЙ вкладки любого сотрудника независимо от экрана (150 запросов в час);
список сотрудников — пять раз в минуту (300 в час); запасной опрос мессенджера
при упавшем потоке — раз в десять секунд; отметка присутствия в чате — раз в
пять, и она вдобавок показывала коллегам «он в чате» там, где на чат никто не
смотрит.

Десять забытых вкладок на фирму превращали это в тысячи запросов в час ни за
чем — круглосуточно, потому что вкладку не закрывают, её сворачивают.

**Почему проверка механическая.** В проекте это случилось четыре раза подряд, и
каждый раз приём стоял в соседних строках того же файла: сердцебиение
присутствия спрашивало видимость с самого начала. Значит следующий таймер
напишут так же — не по злому умыслу, а потому что «просто раз в минуту
обновлять» выглядит безобидно. Один раз это уже уронило боевое обновление:
команда, безобидная в руках человека, из цикла отрисовки дала 240 запросов в
час.

Проверяется КЛАСС, а не список мест: **каждый** повторяющийся таймер обязан либо
спрашивать `visibilityState`, либо нести рядом отметку «таймер-без-сети» с
доводом. Под подозрением все, потому что сетевой вызов почти всегда спрятан за
функцией, и отличить его чтением нельзя.
"""

import pathlib
import re

KOREN = pathlib.Path(__file__).resolve().parent.parent
ISHODNIKI = KOREN / "web" / "frontend" / "crm" / "src"

#: Начало и конец эффекта в этом коде: `useEffect(() => {` … `}, [зависимости]);`
#:
#: Смотрим ИМЕННО тело своего эффекта, а не окно строк вокруг. Первая версия
#: брала двадцать строк вниз и десять вверх — и засчитывала проверку из
#: СОСЕДНЕГО эффекта: подрыв в `lib/app.tsx` не покраснел, потому что рядом
#: стоит сердцебиение присутствия, которое видимость спрашивает. Сторож,
#: засчитывающий чужую проверку, стережёт не то место.
NACHALO = re.compile(r"useEffect\(")
KONETS = re.compile(r"^\s*\}, \[")

#: Отметка «этот таймер в сеть не ходит» — прямо в теле эффекта, с доводом.
#:
#: **Список исключений отдельным файлом заведён не был, и это решение.** Ключом
#: пришлось бы делать путь со строкой, а строки едут от любой правки выше; ключом
#: по имени переменной — переименование. Отметка живёт РЯДОМ с местом, едет
#: вместе с ним и читается тем, кто правит таймер, а не тем, кто открыл список.
#:
#: Пишется так:
#:
#:     // таймер-без-сети: только счёт секунд на экране
#:
#: Признак «ходит в сеть» НЕ выводится из текста: сетевой вызов почти всегда
#: спрятан за функцией (`load()`, `refreshStorage()`, `dochitat()`), и первая
#: версия этой проверки искала `api.get` в теле эффекта — и не нашла НИ ОДНОГО
#: из четырёх мест, ради которых писалась. Поэтому под подозрением каждый
#: повторяющийся таймер, а исключение объявляется вслух.
OTMETKA = "таймер-без-сети:"


def _fayly():
    for put in sorted(ISHODNIKI.rglob("*.ts")) + sorted(ISHODNIKI.rglob("*.tsx")):
        yield put


def _telo_effekta(stroki: list[str], nomer: int) -> str:
    """Тело эффекта, внутри которого стоит таймер со строки `nomer`."""
    nachalo = 0
    for i in range(nomer, -1, -1):
        if NACHALO.search(stroki[i]):
            nachalo = i
            break
    konets = len(stroki)
    for i in range(nomer, len(stroki)):
        if KONETS.match(stroki[i]):
            konets = i + 1
            break
    return "\n".join(stroki[nachalo:konets])


def _tajmery():
    """Места с `setInterval` и телом эффекта, которому таймер принадлежит."""
    for put in _fayly():
        stroki = put.read_text(encoding="utf-8").splitlines()
        for nomer, stroka in enumerate(stroki):
            if "setInterval" not in stroka:
                continue
            yield (
                put.relative_to(KOREN).as_posix(),
                nomer + 1,
                _telo_effekta(stroki, nomer),
            )



PARY = {"(": ")", "[": "]", "{": "}"}


def _konets_vyrazheniya(tekst: str, i: int, ostanovka: str) -> int:
    """Индекс первого знака из `ostanovka` на НУЛЕВОЙ глубине скобок."""
    glubina = 0
    while i < len(tekst):
        z = tekst[i]
        if z in PARY:
            glubina += 1
        elif z in ")]}":
            if glubina == 0:
                return i
            glubina -= 1
        elif glubina == 0 and z in ostanovka:
            return i
        i += 1
    return len(tekst)


def telo_tajmera(telo_effekta: str) -> str:
    """Тело функции, переданной в `setInterval`, — а НЕ всего эффекта.

    Судить по телу эффекта было нельзя: рядом с таймером стоит
    `const vidno = () => document.visibilityState === "visible"`, и слово
    находилось даже тогда, когда таймер его не звал. Снять `if (vidno())` из
    таймера — самая естественная правка, и она сторожа не красила.
    """
    nayd = telo_effekta.find("setInterval(")
    if nayd < 0:
        return ""
    nachalo = nayd + len("setInterval(")
    konets = _konets_vyrazheniya(telo_effekta, nachalo, ",")
    return telo_effekta[nachalo:konets]


def _imena(tekst: str) -> set[str]:
    """Имена, за которыми стоит сходить: вызванные и голое имя целиком.

    Голое нужно потому, что в таймер часто передают именованную функцию:
    `window.setInterval(ping, HEARTBEAT_MS)`. Без него разбор объявлял
    нарушителями два места, где всё сделано правильно.
    """
    imena = set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", tekst))
    golyy = tekst.strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", golyy):
        imena.add(golyy)
    return imena


def sprashivaet_vidimost(
    telo_effekta: str, telo_kolbeka: str, poseshcheno: set[str] | None = None
) -> bool:
    """Спрашивает ли САМ таймер, видна ли вкладка.

    Прямо в теле или через цепочку функций, объявленных в том же эффекте:
    приём в этом коде — `const vidno = () => …`, `const dogonyat = () =>
    { if (vidno()) … }` и `setInterval(dogonyat, …)`. Один шаг цепочки
    разбор уже проходил, двух не проходил — и звал правильное неправильным.
    """
    if "visibilityState" in telo_kolbeka:
        return True
    poseshcheno = set() if poseshcheno is None else poseshcheno
    for imya in _imena(telo_kolbeka):
        if imya in poseshcheno:
            continue
        poseshcheno.add(imya)
        opr = re.search(
            rf"\b(?:const|let|var|function)\s+{re.escape(imya)}\b", telo_effekta
        )
        if opr is None:
            continue
        konets = _konets_vyrazheniya(telo_effekta, opr.end(), ";")
        obyavlenie = telo_effekta[opr.start():konets]
        if sprashivaet_vidimost(telo_effekta, obyavlenie, poseshcheno):
            return True
    return False


def test_perebor_nahodit_tajmery():
    """Сторож, ничего не нашедший, зеленеет на любой беде."""
    naydeno = list(_tajmery())
    assert len(naydeno) >= 5, (
        f"в исходниках нашлось {len(naydeno)} таймеров — сменился способ их "
        "писать, и проверка ниже стерегла бы пустоту"
    )


def test_telo_effekta_ne_zahvatyvaet_sosedniy():
    """Сторож на сам разбор: иначе он засчитает чужую проверку.

    Проверено подрывом: с окном в тридцать строк два подрыва из трёх не
    покраснели — рядом стояли эффекты, которые видимость спрашивают.
    """
    tela = {
        (put, nomer): telo for put, nomer, telo in _tajmery()
    }
    assert tela, "таймеров не нашлось вовсе"
    for (put, nomer), telo in tela.items():
        assert telo.count("useEffect(") <= 1, (
            f"{put}:{nomer}: в тело эффекта попал соседний — разбор захватывает "
            "лишнее, и проверка видимости может быть засчитана чужая"
        )


#: Образец эффекта, у которого таймер видимость НЕ спрашивает, а сосед —
#: спрашивает. Ровно этот вид кода первую редакцию проверки и обманул.
BEZ_PROVERKI_V_TAJMERE = """  useEffect(() => {
    const vidno = () => document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      load(true);
    }, SVODKA_POLL_MS);
    const vernulis = () => {
      if (vidno()) load(true);
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [load]);"""

#: Он же, но с проверкой на месте.
S_PROVERKOY_V_TAJMERE = """  useEffect(() => {
    const vidno = () => document.visibilityState === "visible";
    const timer = window.setInterval(() => {
      if (vidno()) load(true);
    }, SVODKA_POLL_MS);
    const vernulis = () => {
      if (vidno()) load(true);
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [load]);"""


def test_razbor_ne_zaschityvaet_proverku_soseda():
    """Сторож на сам разбор, и он выстрадан.

    Судили по телу эффекта целиком — и `visibilityState` находился в
    определении `vidno`, даже когда таймер его не звал. Снять `if (vidno())`
    из таймера, оставив слушателя `visibilitychange`, — самая естественная
    правка, и она не красила ни одного сторожа.
    """
    assert not sprashivaet_vidimost(
        BEZ_PROVERKI_V_TAJMERE, telo_tajmera(BEZ_PROVERKI_V_TAJMERE)
    ), "разбор засчитал проверку соседа: таймер видимость не спрашивает"
    assert sprashivaet_vidimost(
        S_PROVERKOY_V_TAJMERE, telo_tajmera(S_PROVERKOY_V_TAJMERE)
    ), "разбор не увидел проверки, стоящей в самом таймере"


def test_telo_tajmera_ne_zahvatyvaet_srok():
    """Тело таймера кончается на запятой перед сроком, а не на первой в скобках."""
    telo = telo_tajmera(S_PROVERKOY_V_TAJMERE)
    assert "SVODKA_POLL_MS" not in telo, "в тело таймера попал срок"
    assert "load(true)" in telo, "тело таймера не нашлось вовсе"
    assert "vernulis" not in telo, "в тело таймера попал соседний обработчик"

def test_povtoryayushchiysya_tajmer_sprashivaet_vidna_li_vkladka():
    vinovnye = []
    for put, nomer, telo in _tajmery():
        if OTMETKA in telo:
            continue
        if sprashivaet_vidimost(telo, telo_tajmera(telo)):
            continue
        vinovnye.append(f"{put}:{nomer}")

    assert not vinovnye, (
        "повторяющийся запрос идёт из ФОНОВОЙ вкладки: "
        + ", ".join(vinovnye)
        + ".\nСвёрнутую вкладку не закрывают — её забывают, и она ходит на "
        "сервер круглосуточно. Спросите `document.visibilityState` в теле "
        "таймера и перечитайте на `visibilitychange`, чтобы вернувшийся человек "
        "увидел свежее сразу. Приём есть рядом: сердцебиение присутствия в "
        "`lib/app.tsx` и сводка в `screens/Dashboard.tsx`"
    )


def test_u_otmetki_est_dovod():
    """Отметка без довода — это «потому что» без «почему».

    Голое слово отключает проверку и ничего не объясняет; через полгода его
    скопируют в следующий таймер, не думая. Требуем текст после двоеточия.
    """
    pustye = []
    for put, nomer, telo in _tajmery():
        if OTMETKA not in telo:
            continue
        for stroka in telo.splitlines():
            if OTMETKA in stroka and len(stroka.split(OTMETKA, 1)[1].strip()) < 10:
                pustye.append(f"{put}:{nomer}")
    assert not pustye, (
        "у отметки «таймер-без-сети» нет довода: "
        + ", ".join(pustye)
        + ". Напишите, почему этот таймер серверу ничего не стоит"
    )

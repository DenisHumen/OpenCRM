"""Экран веб-почты (`screens/Mail.tsx`): правила, которые держат чужое письмо в рамке.

Проверка по исходнику, как в `test_screens.py`: собранного фронтенда в наборе
нет. Каждое правило — на беду, которую в почте не прощают: письмо — это HTML от
кого угодно, и один пропущенный атрибут превращает его в скрипт внутри CRM.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "web" / "frontend" / "crm" / "src"
EKRAN = (SRC / "screens" / "Mail.tsx").read_text(encoding="utf-8")


def _ramki() -> list[str]:
    return re.findall(r"<iframe\b(.*?)/>", EKRAN, re.S)


def test_pismo_risuetsya_v_ramke_bez_skriptov():
    """Без `allow-scripts` и `allow-same-origin`: вместе они снимают песочницу
    целиком, а по отдельности дают скрипту письма либо выполниться, либо
    дотянуться до cookie CRM."""
    ramki = _ramki()
    assert len(ramki) >= 2, "рамки письма и архива исчезли — проверка смотрит не туда"
    for ramka in ramki:
        pesochnitsa = re.search(r'sandbox="([^"]*)"', ramka)
        assert pesochnitsa, "у рамки письма нет sandbox — письмо живёт в документе CRM"
        assert "allow-scripts" not in pesochnitsa.group(1)
        assert "allow-same-origin" not in pesochnitsa.group(1)
        if "srcDoc=" in ramka:
            # Архив CRM: своей страницы нет, документ собирает ramkaArhiva со своей
            # политикой (test_ramka_arhiva_so_svoey_politikoy).
            assert "srcDoc={ramkaArhiva(" in ramka, "srcDoc рамки собран мимо ramkaArhiva"
        else:
            # Адресом своей страницы, а не `srcDoc`: рамка с `srcDoc` наследует политику
            # CRM, и картинки по кнопке не показывались вовсе (проба 09.10.2026).
            assert "/html?folder=" in ramka, "письмо должно приходить страницей со своей политикой"
    assert any("/html?folder=" in r for r in ramki), "письмо с сервера ушло из страницы со своей политикой"


def _telo(imya: str, tekst: str = EKRAN) -> str:
    """Текст функции или стрелки `imya` — до закрывающей скобки её уровня."""
    m = re.search(rf"^( *)(?:export )?(?:function {imya}\(|const {imya} = )", tekst, re.M)
    assert m, f"{imya} не нашлась — проверка смотрит не туда"
    otstup = m.group(1)
    konets = re.compile(rf"^{otstup}\}}\)?;?$", re.M).search(tekst, m.end())
    assert konets, f"конец {imya} не нашёлся"
    return tekst[m.start() : konets.end()]


def test_ramka_arhiva_so_svoey_politikoy():
    """`srcDoc` наследует политику CRM, где можно грузить с нашего адреса. Документ
    рамки архива несёт свою: ни скриптов, ни запросов, картинки только `data:`."""
    telo = _telo("ramkaArhiva")
    assert "Content-Security-Policy" in telo
    assert "default-src 'none'; img-src data:;" in telo
    assert "script-src" not in telo and "https:" not in telo


def test_vnutri_ramki_svoya_politika():
    """Политика страницы письма — своя, заголовком: ни скриптов, ни запросов,
    ни форм; картинки только data:, а https: — лишь по кнопке."""
    from core.services import webmail_service

    bez = webmail_service.CSP_PISMA.format(https="")
    s_kartinkami = webmail_service.CSP_PISMA.format(https=" https:")
    assert "default-src 'none'" in bez and "script-src" not in bez
    assert "img-src data:;" in bez
    assert "img-src data: https:;" in s_kartinkami
    assert "form-action 'none'" in bez and "frame-ancestors 'self'" in bez
    assert "allow-scripts" not in bez and "allow-same-origin" not in bez


def test_chuzhaya_razmetka_ne_vstavlyaetsya_v_dokument():
    """HTML письма не попадает в документ CRM ни через `dangerouslySetInnerHTML`,
    ни через `innerHTML` чего-либо, кроме редактора своего письма — а туда он
    идёт только после чистки (`dlyaRedaktora`)."""
    assert "dangerouslySetInnerHTML" not in EKRAN
    prisvoeniya = re.findall(r"\.innerHTML\s*=\s*([^;]+);", EKRAN)
    assert prisvoeniya, "редактор письма перестал заполняться — проверка смотрит не туда"
    for chto in prisvoeniya:
        assert chto.strip() == "nachalo.html", f"в документ CRM уходит разметка мимо начала письма: {chto}"
    # Начало письма из чужого HTML собирается только через чистку.
    assert "html: p.html ? dlyaRedaktora(p.html)" in EKRAN


def test_vneshnie_kartinki_tolko_po_knopke():
    """Картинка из интернета — это «письмо открыли» для отправителя. Показывать
    её можно только по нажатию, и только https."""
    assert "&images=${kartinki ? 1 : 0}" in EKRAN


def test_kartochki_prodolzhayut_pisat_iz_pochty():
    """Карточки клиента и заявки берут окно отправки отсюда: потеря экспорта —
    это белый экран карточки, а не ошибка сборки почты."""
    assert "export function MailCompose(" in EKRAN
    assert "export interface MailSender" in EKRAN
    for kartochka in ("ClientCard.tsx", "DealCard.tsx"):
        tekst = (SRC / "screens" / kartochka).read_text(encoding="utf-8")
        assert 'import { MailCompose, type MailSender } from "./Mail";' in tekst


def test_chernovik_ne_chashche_pauzy():
    """Автосохранение черновика — запись в IMAP; на каждую букву это сотни
    писем в «Черновиках» за минуту набора."""
    pauza = re.search(r"const PAUZA_CHERNOVIKA = (\d[\d_]*);", EKRAN)
    assert pauza and int(pauza.group(1).replace("_", "")) >= 5000
    assert "useDebounced(snimok, PAUZA_CHERNOVIKA)" in EKRAN


def test_adres_pisma_otkryvaet_pismo():
    """Пуш-уведомление ведёт на `/mail?box=&uid=`: экран обязан читать адрес."""
    for klyuch in ('params.get("box")', 'params.get("folder")', 'params.get("uid")'):
        assert klyuch in EKRAN, f"экран не читает {klyuch} — уведомление откроет пустую почту"


def test_kody_veb_pochty_podpisany():
    """Отказы веб-почты подписаны словами интерфейса (карта `lib/oshibki.ts`)."""
    karta = (SRC / "lib" / "oshibki.ts").read_text(encoding="utf-8")
    for kod in ("mail_box_forbidden", "mail_folder_bad", "mail_folder_protected", "mail_message_gone",
                "mail_upload_too_big", "mail_upload_missing"):
        assert re.search(rf"^  {kod}: \"err\w+\",", karta, re.M), f"код {kod} без подписи"


def test_pismo_ne_shlyot_telo_dvazhdy():
    """Разбор 10.10.2026: экран слал и text, и html — тело дважды. Письмо со
    вставленным снимком экрана упиралось в предел запроса в 1 МБ
    (`body_too_large`): не уходило, не сохранялось, окно не закрывалось."""
    telo = _telo("telo")
    assert "html.trim() ? { html } : { text:" in telo, "текст и разметка снова уходят вместе"
    assert not re.search(r"^\s*text: redaktor", telo, re.M)


def test_vstavlennye_kartinki_uhodyat_vlozheniyami():
    """Картинка строкой `data:` в теле — тот же предел в 1 МБ. Её уносят во
    вложения через загрузку (`/mail/boxes/uploads`) и при вводе, и перед отправкой
    (Ctrl+Enter не уводит фокус из редактора)."""
    vynesti = _telo("vynestiKartinki")
    assert "data:" in vynesti and "dobavitFayly(" in vynesti and ".remove()" in vynesti
    assert "vynestiKartinki()" in _telo("prinyatRedaktor")
    otpravit = _telo("otpravit")
    assert otpravit.index("vynestiKartinki()") < otpravit.index("guard.take()")
    assert '"/mail/boxes/uploads"' in _telo("dobavitFayly")
    assert "onPaste=" in EKRAN[EKRAN.index('className="wm-editor"') :]


def test_otpravka_zhdyot_chernovik_i_glushit_avtosokhranenie():
    """Разбор 10.10.2026: отправка не ждала идущего сохранения черновика и не
    останавливала автосохранение. Письмо уходило адресату, а его полная копия
    оставалась в «Черновиках», и по ней легко отправить второй раз."""
    otpravit = _telo("otpravit")
    poslat = otpravit.index("/send`")
    zaglushka = otpravit.index('zakryto.current = "otpravleno"')
    ozhidanie = otpravit.index("await idushchee.current")
    assert zaglushka < ozhidanie < poslat, "автосохранение глушится после отправки или не ждётся вовсе"
    # Провал отправки возвращает автосохранение: письмо остаётся в работе.
    assert 'zakryto.current = "net"' in otpravit[poslat:]
    sokhranit = _telo("sokhranitChernovik")
    assert sokhranit.index('zakryto.current === "otpravleno"') < sokhranit.index("api.post")
    assert "idushchee.current = new Promise" in sokhranit
    # Повтор из очереди не пишет черновик поверх уходящего письма.
    assert 'zakryto.current === "net" && poslednee.current.snimok' in sokhranit
    zakryt = _telo("zakryt")
    assert zakryt.index('zakryto.current === "otpravleno"') < zakryt.index("sokhranitChernovik()")


def test_signal_novoy_pochty_po_uidnext_s_metkoy_pusha():
    """Разбор 10.10.2026: сигнал звенел на рост непрочитанных — их растит и
    коллега, пометивший письмо непрочитанным. А окно вкладки с чужой меткой
    вставало вторым рядом с пушем того же письма."""
    from core.services import mail_watcher

    prinyat = _telo("prinyatYashchiki")
    assert "inbox_uidnext" in prinyat and "b.unseen <= ran" not in prinyat
    assert "teper.validity !== ran.validity" in prinyat, "смена UIDVALIDITY сочтена новым письмом"
    assert "tag: `opencrm-mail-${b.id}`" in prinyat
    istochnik = pathlib.Path(mail_watcher.__file__).read_text(encoding="utf-8")
    assert 'tag=f"opencrm-mail-{box.id}"' in istochnik, "метка пуша почты разошлась с меткой окна вкладки"
    signaly = (SRC / "lib" / "signaly.ts").read_text(encoding="utf-8")
    assert "tag: opts.tag ??" in signaly, "сигнал не принимает метку окна"


def test_arhiv_crm_v_veb_pochte():
    """Архив CRM — копия писем в базе: у каждого ящика свой пункт, список с
    поиском, письмо, сырое .eml, вложения и отметка «удалено на сервере».
    Пункт стоит вне списка папок: папки идут с сервера, а архив нужен как раз
    тогда, когда сервер молчит."""
    for put in ("/mail/archive?", "/mail/archive/${aid}", "/mail/archive/${pismo.id}/raw",
                "/mail/archive/${pismo.id}/attachments/"):
        assert put in EKRAN, f"архив не ходит в {put}"
    assert 'params.get("arhiv")' in EKRAN and 'params.get("aid")' in EKRAN
    assert "removed_at" in EKRAN and 'p.set("removed", "1")' in EKRAN
    papki = EKRAN.index("{papkiYashchika && (")
    punkt = EKRAN.index("onClick={() => otkrytArhiv(b.id)}")
    konets_papok = EKRAN.index("</ul>\n                  )}", papki)
    assert punkt > konets_papok, "пункт архива спрятан в списке папок: без сервера до него не дойти"


def test_arhiv_hodit_v_zhivye_ruchki():
    """Экран и ручки архива писались порознь: путь, которого нет на сервере, —
    «не найдено» вместо письма, которое как раз и пережило сервер."""
    from web.api.routes import mail_archive

    puti = {r.path for r in mail_archive.router.routes}
    for put in ("/mail/archive", "/mail/archive/stats", "/mail/archive/{archive_id}",
                "/mail/archive/{archive_id}/raw", "/mail/archive/{archive_id}/attachments/{part}"):
        assert put in puti, f"экран ходит в {put}, а ручки нет"
    server = (SRC / "screens" / "MailServer.tsx").read_text(encoding="utf-8")
    assert '"/mail/archive/stats"' in server, "карточка сервера не показывает, сколько писем в копии"

r"""Публичный файл работы не выпускает за каталог работ.

Прежняя защита была чёрным списком: на входе проверяли, нет ли в `work_uid`
разделителей `/` и `\`. Такой список всегда неполон — он перечисляет то, о чём
вспомнили, — и `..` через него проходил: `media_dir/../large.webp` уже не
каталог работ.

Проверка переехала туда, где путь собирается, и спрашивает не «нет ли в имени
чего-то плохого», а «лежит ли получившийся файл внутри каталога работ». У этого
вопроса однозначный ответ, и он не зависит от изобретательности спрашивающего.
"""

import pytest

from core.services import media_service


@pytest.fixture()
def katalog_rabot(tmp_path, monkeypatch):
    """Каталог работ во временной папке, рядом — посторонний файл."""
    media = tmp_path / "works"
    (media / "rabota-1").mkdir(parents=True)
    (media / "rabota-1" / "large.webp").write_bytes("свой".encode())
    # сосед каталога работ: именно до него и пытались бы дотянуться
    (tmp_path / "large.webp").write_bytes("чужой".encode())

    nastoyashchie = media_service.get_settings()

    class Podmena:
        media_dir = media

        def __getattr__(self, name):
            return getattr(nastoyashchie, name)

    monkeypatch.setattr(media_service, "get_settings", lambda: Podmena())
    return media


def test_svoy_fayl_otdayotsya(katalog_rabot):
    put = media_service.public_file("rabota-1", "large.webp")
    assert put is not None
    assert put.read_bytes() == "свой".encode()


@pytest.mark.parametrize(
    "work_uid",
    [
        "..",
        "../",
        "rabota-1/..",
        "./..",
        "..\\",
        "rabota-1/../..",
    ],
)
def test_naruzhu_ne_vypuskaet(katalog_rabot, work_uid):
    """Ни одна из этих форм не должна дать путь.

    Часть из них выводит за каталог работ, часть — нет: `rabota-1/..`
    схлопывается обратно в корень. Но отвергаются они все, и по одной причине:
    доступ спрашивают по строке `work_uid`, а файл читают по пути, который из
    неё собран. Пока это одно и то же имя, они не разойдутся; как только в
    строке появляется путь — уже разошлись.

    Формы перечислены не ради полноты списка (полного не бывает), а чтобы
    поймать возврат к проверке по кусочкам имени, если её однажды вернут.
    """
    assert media_service.public_file(work_uid, "large.webp") is None


def test_simvolicheskaya_ssylka_naruzhu_ne_rabotaet(katalog_rabot, tmp_path):
    """Ссылка ИЗНУТРИ каталога работ наружу — тот же выход, только окольный."""
    ssylka = katalog_rabot / "rabota-2"
    try:
        ssylka.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки в этой системе недоступны")
    assert media_service.public_file("rabota-2", "large.webp") is None


def test_imya_fayla_po_prezhnemu_iz_spiska(katalog_rabot):
    """Белый список имён — он-то как раз уместен: имён ровно столько, сколько мы кладём."""
    (katalog_rabot / "rabota-1" / "secret.txt").write_bytes("тайна".encode())
    assert media_service.public_file("rabota-1", "secret.txt") is None

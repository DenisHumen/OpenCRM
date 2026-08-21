"""Снимки товара: приём, два размера на диске, выдача и удаление.

**Зачем блоку склада фотографии.** Название опознаёт вещь плохо: «шлейф 40-pin»
и «шлейф 40-pin (узкий)» — две строки, отличить которые на полке можно только
глазами. Снимок отвечает на вопрос «это она?» за секунду.

--------------------------------------------------------------------------
Два размера, и оба делаются сразу
--------------------------------------------------------------------------

`view` — сам снимок, ужатый до `MAX_STORONA`; `thumb` — плитка в списке.

Хранить оригинал как есть нельзя: телефон снимает на 4–8 МБ, а в карточке
десяток позиций, и список товаров превращался бы в мегабайты трафика на каждое
открытие. Обратная крайность — один маленький размер — тоже неверна: снимок
открывают, чтобы РАССМОТРЕТЬ, и плитка в 320 точек на это не годится.

Оба делаются **сразу, а не по требованию**, и это отличает снимок от работы
доски. У работ обработка фоновая (`media_service`), потому что там видео,
блюрхэш и очередь; здесь — одна картинка и две операции над ней. Отложенная
обработка стоила бы очереди, состояния «обрабатывается» и экрана, который умеет
его показывать, — ради двух вызовов PIL.

--------------------------------------------------------------------------
Кадрируем по длинной стороне, а не в квадрат
--------------------------------------------------------------------------

В отличие от аватара (`avatar_service`), где центральный квадрат безобиден:
у лица центр и есть лицо. У детали центр — это середина детали, и квадрат
отрежет ровно те края, по которым её узнают: разъём, маркировку, длину шлейфа.
Поэтому пропорции сохраняются, а ограничивается длинная сторона.

--------------------------------------------------------------------------
Тип определяется подписью файла, а не расширением
--------------------------------------------------------------------------

Как у аватара и по той же причине: `.jpg` в имени ничего не гарантирует. SVG не
принимается вовсе — это документ со скриптом внутри, а не растр.
"""

import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session
from sqlalchemy import event as sa_event

from config.settings import get_settings
from core import exceptions as errors
from core.services import audit_service, media_service, storage_service
from database.models import ProductPhoto, User
from database.repositories import warehouse as warehouse_repo

#: Длинная сторона снимка, который открывают. 1600 — «рассмотреть деталь на
#: экране ноутбука»; больше не нужно никому, кроме печати, а печатают не отсюда.
MAX_STORONA = 1600
#: Длинная сторона плитки в списке.
MAX_STORONA_PLITKI = 320
#: Потолок принимаемого файла. Тот же, что у аватара: снимок с телефона в него
#: укладывается с запасом, а всё, что больше, — это скан или чужой файл.
MAX_BYTES = 10 * 1024 * 1024
#: Сколько снимков на товар. Не ограничение ради ограничения: карточка с
#: полусотней фотографий перестаёт отвечать на вопрос «это она?».
MAX_NA_TOVAR = 12


def _katalog(product_id: int) -> Path:
    return get_settings().product_photos_dir / str(product_id)


def put_na_diske(photo: ProductPhoto, razmer: str = "view") -> Path:
    """Файл снимка. `view` — сам снимок, `thumb` — плитка."""
    hvost = "-thumb" if razmer == "thumb" else ""
    return _katalog(photo.product_id) / f"{photo.photo_uid}{hvost}.webp"


def _ulozhit(im: Image.Image, storona: int) -> Image.Image:
    """Ужать под длинную сторону, сохранив пропорции. Мелкое не растягиваем."""
    shirina, vysota = im.size
    dlinnaya = max(shirina, vysota)
    if dlinnaya <= storona:
        return im.copy()
    dolya = storona / dlinnaya
    return im.resize((max(1, round(shirina * dolya)), max(1, round(vysota * dolya))), Image.LANCZOS)


def spisok(db: Session, product_id: int) -> list[ProductPhoto]:
    return warehouse_repo.list_product_photos(db, product_id)


def dobavit(
    db: Session, product_id: int, uploader: User, original_name: str, content: bytes
) -> ProductPhoto:
    """Принять снимок: проверить, ужать, положить два файла, записать строку."""
    product = warehouse_repo.get_product(db, product_id)
    if product is None:
        raise errors.NotFoundError("Product not found", code="product_not_found")
    if not content:
        raise errors.ValidationError("File is empty", code="file_empty")
    if len(content) > MAX_BYTES:
        raise errors.ValidationError(
            f"Image is too large (max {MAX_BYTES // (1024 * 1024)} MB)", code="file_too_large"
        )

    vid, _ext, mime = media_service.detect_media(content[:512], original_name or "photo")
    if vid != "image" or mime == "image/svg+xml":
        raise errors.ValidationError(
            "A product photo must be a JPG, PNG, WebP or GIF image", code="bad_photo_type"
        )
    if len(spisok(db, product_id)) >= MAX_NA_TOVAR:
        raise errors.ValidationError(
            f"A product can have at most {MAX_NA_TOVAR} photos", code="too_many_photos"
        )
    if not storage_service.has_room_for(len(content)):
        raise errors.ValidationError(
            "Not enough free disk space on the server", code="disk_full"
        )

    katalog = _katalog(product_id)
    katalog.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    bolshoy = katalog / f"{uid}.webp"
    plitka = katalog / f"{uid}-thumb.webp"
    try:
        with Image.open(BytesIO(content)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert(
                    "RGBA" if "transparency" in im.info or im.mode in ("P", "LA") else "RGB"
                )
            _ulozhit(im, MAX_STORONA).save(bolshoy, "WEBP", quality=85)
            _ulozhit(im, MAX_STORONA_PLITKI).save(plitka, "WEBP", quality=80)
    except errors.AppError:
        raise
    except Exception:
        # Убираем ОБА: половина пары на диске — это снимок, который откроется
        # плиткой и не откроется целиком, и понять почему будет нечем.
        bolshoy.unlink(missing_ok=True)
        plitka.unlink(missing_ok=True)
        raise errors.ValidationError("Could not read image", code="bad_image") from None

    photo = ProductPhoto(
        product_id=product_id,
        uploaded_by=uploader.id if uploader else None,
        photo_uid=uid,
        original_name=Path(original_name or "photo").name[:255],
        # Вес обоих файлов: показанный размер обязан совпасть с тем, что
        # освободится при удалении.
        size_bytes=bolshoy.stat().st_size + plitka.stat().st_size,
        # В конец списка. Порядок задаёт человек, а не время загрузки, — но
        # начинать откуда-то надо, и «последним» удивляет меньше всего.
        sort_order=warehouse_repo.next_photo_order(db, product_id),
    )
    db.add(photo)
    db.flush()
    storage_service.invalidate_size_cache()
    return photo


def poluchit(db: Session, product_id: int, photo_id: int) -> ProductPhoto:
    photo = warehouse_repo.get_product_photo(db, product_id, photo_id)
    if photo is None:
        raise errors.NotFoundError("Photo not found", code="photo_not_found")
    return photo


def udalit(db: Session, product_id: int, photo_id: int, actor: User) -> None:
    """Убрать снимок. Файлы — ПОСЛЕ фиксации, как у файлов клиента.

    Порядок здесь не педантизм: откат транзакции вернул бы строку, а файлов уже
    не было бы — карточка обещала бы снимок, которого физически нет, и открытие
    падало бы. Сирота на диске (обратный случай) стоит места, а не правды, и
    видна менеджеру файлов.
    """
    photo = poluchit(db, product_id, photo_id)
    puti = [put_na_diske(photo, "view"), put_na_diske(photo, "thumb")]
    audit_service.record_deletion(
        db,
        actor=actor,
        entity_type=audit_service.ENTITY_FILE,
        entity_id=photo.id,
        entity_label=photo.original_name,
    )
    db.delete(photo)
    db.flush()

    @sa_event.listens_for(db, "after_commit", once=True)
    def _snyat_s_diska(_session) -> None:
        for put in puti:
            put.unlink(missing_ok=True)
        storage_service.invalidate_size_cache()


def perestavit(db: Session, product_id: int, poryadok: list[int]) -> list[ProductPhoto]:
    """Задать порядок снимков. Первый — тот, что показывают везде.

    Принимаем ПОЛНЫЙ порядок, а не «подвинуть этот на одну позицию»: частичная
    перестановка требует знать, что было до неё, и два человека, двигающие
    соседние снимки, получили бы порядок, которого не задавал ни один.
    """
    photos = {p.id: p for p in spisok(db, product_id)}
    neizvestnye = [i for i in poryadok if i not in photos]
    if neizvestnye:
        raise errors.ValidationError(
            "Unknown photo in the order", code="photo_not_found"
        )
    if len(set(poryadok)) != len(photos):
        raise errors.ValidationError(
            "The order must list every photo of the product exactly once",
            code="bad_photo_order",
        )
    for mesto, photo_id in enumerate(poryadok):
        photos[photo_id].sort_order = mesto
    db.flush()
    return spisok(db, product_id)

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from database.session import Base

# Ключи настроек сайта и значения по умолчанию.
SETTING_DEFAULTS: dict[str, str] = {
    "brand_name": "Studio",
    "brand_tagline": "",
    "brand_logo_path": "",
    "accent_color": "#D97757",
    "contact_email": "",
    "contact_phone": "",
    "social_telegram": "",
    "social_instagram": "",
    "social_website": "",
    "showcase_locale": "en",  # en | ru
    "og_default_image": "",
    # строка «7 works · updated 28.07.2026» под заголовком доски: "1" — показывать.
    # По умолчанию выключено — клиенту важны работы, а не счётчик
    "showcase_show_meta": "0",
    # футер витрины с контактами студии и «Curated by»: "1" — показывать
    "showcase_show_footer": "0",
    # сайт студии: пусто — кнопки «Return to the site» в шапке витрины нет
    "studio_site_url": "",
    # лого сайта, на который ведёт кнопка; пусто — кнопка остаётся только с текстом
    "studio_site_logo": "",
    # Ручное обслуживание: "1" — все, кроме root, видят заглушку. Это НЕ то же
    # самое, что заглушка nginx: та показывается, когда приложения нет вовсе, а
    # эта — когда оно работает, но root закрыл его на работы и сам продолжает им
    # пользоваться.
    "maintenance_mode": "0",
    # необязательное пояснение на заглушке: «вернёмся к 14:00», «переезд базы»
    "maintenance_note": "",
    # когда включили и кто — чтобы забытый режим было видно и понятно, к кому идти
    "maintenance_since": "",
    "maintenance_by": "",
    # подпись на кнопке возврата. Пусто — берётся перевод по языку витрины
    # («Return to the site» / «Вернуться на сайт»): у студии может быть своё
    # слово, но по умолчанию подпись обязана следовать языку страницы, а не
    # застывать на английском.
    "studio_site_label": "",
}


class SiteSetting(Base):
    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

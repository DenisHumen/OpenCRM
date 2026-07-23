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
}


class SiteSetting(Base):
    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

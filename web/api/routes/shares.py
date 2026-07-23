from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.services import share_service
from database.models import User
from database.repositories import shares as shares_repo
from web.api import schemas
from web.api.deps import get_db, require_staff

router = APIRouter(prefix="/shares", tags=["shares"])


@router.patch("/{share_id}")
def update_share(
    share_id: int,
    payload: schemas.SharePatchIn,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    fields = payload.model_fields_set
    link = share_service.update_share(
        db,
        share_id,
        is_active=payload.is_active if "is_active" in fields else share_service.UNSET,
        expires_at=payload.expires_at if "expires_at" in fields else share_service.UNSET,
        pin=payload.pin if "pin" in fields else share_service.UNSET,
    )
    return schemas.share_out(link, share_service.share_stats(db, link))


@router.delete("/{share_id}")
def delete_share(
    share_id: int,
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    share_service.delete_share(db, share_id)
    return {"message": "Share link deleted"}


@router.post("/{share_id}/regenerate")
def regenerate_share(
    share_id: int,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    link = share_service.regenerate(db, share_id, user)
    return schemas.share_out(link, {"views_count": 0, "unique_views_count": 0, "last_viewed_at": None})


@router.get("/{share_id}/views")
def share_views(
    share_id: int,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    link = share_service.get_share(db, share_id)
    views, total = shares_repo.list_views(db, share_id, page=page, per_page=per_page)
    result = schemas.paginated([schemas.view_out(v) for v in views], total, page, per_page)
    result.update(share_service.share_stats(db, link))
    return result

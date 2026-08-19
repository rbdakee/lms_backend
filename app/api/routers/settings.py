from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AdminSettingsOut, AdminSettingsPatchIn, PublicSettingsOut
from app.application.settings import SettingsService

router = APIRouter()
admin_router = APIRouter(prefix="/admin")

# Сутки: логотип меняют раз в год, а стоит он на каждой странице лендинга
BRANDING_CACHE = "public, max-age=86400"

# Логотипом кладут и svg, а внутри svg бывает <script>. Открытый прямым
# адресом, он выполнился бы на домене API — том самом, чью куку сессии делят
# оба фронта. В <img> картинка от этих заголовков не страдает, а скрипту
# внутри неё браузер не даёт ни источников, ни собственного кода.
BRANDING_SECURITY = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'",
}


@router.get("/settings")
def public_settings(
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
) -> PublicSettingsOut:
    # Без входа: название, логотип и контакты показывает лендинг
    return PublicSettingsOut(**svc.public())


@router.get("/branding/{slot}")
def branding_image(
    slot: str,
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
) -> StreamingResponse:
    # Слот — обычная строка, а не перечисление: выдуманное имя должно давать
    # общий 404, как и не поставленная картинка, а не 422 от валидатора пути.
    # content-disposition не ставим — картинка стоит в <img>, а не скачивается
    mime, size, chunks = svc.content(slot)
    return StreamingResponse(
        chunks,
        media_type=mime,
        headers={
            "content-length": str(size),
            "cache-control": BRANDING_CACHE,
            **BRANDING_SECURITY,
        },
    )


@admin_router.get("/settings")
def admin_settings(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
) -> AdminSettingsOut:
    return AdminSettingsOut(**svc.admin_get())


@admin_router.patch("/settings")
def patch_settings(
    body: AdminSettingsPatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
) -> AdminSettingsOut:
    # exclude_unset насквозь: «поля нет» и «прислали null» — разные случаи,
    # и certificate_images с одним ключом не должен гасить остальные картинки
    return AdminSettingsOut(**svc.patch(body.model_dump(exclude_unset=True)))

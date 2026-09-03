from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import AdminSettingsOut, AdminSettingsPatchIn, PublicSettingsOut
from app.application.settings import SettingsService

router = APIRouter()
admin_router = APIRouter(prefix="/admin")

# Сутки: логотип меняют выкаткой, а стоит он на каждой странице лендинга.
#
# `vary: origin` к нему обязателен, и ставим мы его сами: адрес картинки один
# на обе площадки, а байты у них разные — кэш без этого заголовка отдал бы
# второй площадке логотип первой. CORSMiddleware дописывает `vary` только
# тем ответам, у которых в запросе был `Origin`, а здесь он бывает и без него.
BRANDING_CACHE = "public, max-age=86400"
BRANDING_VARY = "origin"

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
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
) -> PublicSettingsOut:
    # Без входа: название, логотип и контакты показывает лендинг — свои
    # у каждой площадки
    return PublicSettingsOut(**svc.public(platform))


@router.get("/branding/{slot}")
def branding_image(
    slot: str,
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[SettingsService, Depends(deps.get_settings_service)],
    requested: Annotated[str | None, Query(alias="platform")] = None,
) -> StreamingResponse:
    # Слот и код площадки — обычные строки, а не перечисления: выдуманное имя
    # должно давать общий 404, как и не положенная картинка, а не 422
    # от валидатора. content-disposition не ставим — картинка стоит в <img>,
    # а не скачивается.
    #
    # Параметр главнее `Origin`, и он же тут главный способ: картинку тянет
    # `<img>`, а в такой запрос браузер `Origin` не кладёт вовсе — без
    # параметра вторая площадка получала бы логотип первой. Адрес с параметром
    # собирает сервер (`GET /settings`), фронт его не сочиняет; подделка
    # безобидна — картинка и так публичная. Параметра нет — прежнее поведение,
    # площадка из `Origin`, и адрес первой площадки работает как работал.
    mime, size, chunks = svc.content(requested or platform, slot)
    return StreamingResponse(
        chunks,
        media_type=mime,
        headers={
            "content-length": str(size),
            "cache-control": BRANDING_CACHE,
            "vary": BRANDING_VARY,
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

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.adapters.db.models import User
from app.api import deps
from app.api.pagination import PageParams, page_out
from app.api.routers.admin import platform_filter
from app.api.schemas import (
    AdminCertificateCardOut,
    AdminCertificatesPageOut,
    CertificateIssueIn,
    CertificatePatchIn,
)
from app.application.certificates_admin import CertificatesAdminService
from app.domain.errors import ValidationAppError

router = APIRouter(prefix="/admin")

# Вкладки списка: «Ждут выдачи», «Выданные», «Отозванные».
CERTIFICATE_FILTER_STATUSES = {"requested", "issued", "revoked"}


def _status(raw: str | None) -> str | None:
    """Вкладка в фильтр запроса. Параметра нет — все три состояния сразу.

    Разбор здесь, а не `Literal` в сигнатуре: валидатор FastAPI ответил бы
    английским текстом, а этот отказ читает админ — та же причина, что
    у `platform_filter`.
    """
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if value not in CERTIFICATE_FILTER_STATUSES:
        raise ValidationAppError(f"Неизвестный статус сертификата: {value}")
    return value


@router.get("/certificates")
def admin_certificates(
    admin: Annotated[User, Depends(deps.get_current_admin)],
    params: Annotated[PageParams, Depends()],
    svc: Annotated[CertificatesAdminService, Depends(deps.get_certificates_admin_service)],
    status: Annotated[str | None, Query()] = None,
    # Одно значение; параметра нет — обе площадки
    platform: Annotated[str | None, Query()] = None,
    # Поиск по ФИО, ИИН и обоим номерам — по всему, что видно в строке
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> AdminCertificatesPageOut:
    data = svc.admin_list(
        status=_status(status),
        platform=platform_filter(platform),
        q=q,
        offset=params.offset,
        limit=params.per_page,
    )
    return AdminCertificatesPageOut(**page_out(data["items"], data["total"], params))


@router.get("/certificates/{certificate_id}")
def admin_certificate(
    certificate_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CertificatesAdminService, Depends(deps.get_certificates_admin_service)],
) -> AdminCertificateCardOut:
    return AdminCertificateCardOut(**svc.card(certificate_id))


@router.post("/certificates/{certificate_id}/issue")
def issue_certificate(
    certificate_id: int,
    body: CertificateIssueIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CertificatesAdminService, Depends(deps.get_certificates_admin_service)],
) -> AdminCertificateCardOut:
    # Ответ — карточка целиком: в ней появились номер и дата выдачи, и экран
    # перерисовывается ответом, а не вторым запросом
    return AdminCertificateCardOut(
        **svc.issue(admin, certificate_id, body.registration_number)
    )


@router.patch("/certificates/{certificate_id}")
def patch_certificate(
    certificate_id: int,
    body: CertificatePatchIn,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CertificatesAdminService, Depends(deps.get_certificates_admin_service)],
) -> AdminCertificateCardOut:
    # exclude_unset: неприсланное поле — «не трогать», а не «стереть»;
    # различить их иначе нельзя, у половины полей null осмыслен
    return AdminCertificateCardOut(
        **svc.patch(certificate_id, body.model_dump(exclude_unset=True))
    )


@router.post("/certificates/{certificate_id}/revoke")
def revoke_certificate(
    certificate_id: int,
    admin: Annotated[User, Depends(deps.get_current_admin)],
    svc: Annotated[CertificatesAdminService, Depends(deps.get_certificates_admin_service)],
) -> AdminCertificateCardOut:
    # POST, а не DELETE: отзыв — парный «Выдать», строка никуда не девается,
    # а ответом уходит карточка целиком, которой у DELETE с его 204 быть
    # не может
    return AdminCertificateCardOut(**svc.revoke(admin, certificate_id))

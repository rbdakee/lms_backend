from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.adapters.db.models import User
from app.api import deps
from app.api.schemas import CertificateOut, CompletionOut, MyCertificatesOut, VerifyOut
from app.application.certificates import CertificatesService

# Чек-лист и выдача висят на курсе, список — в кабинете учителя,
# проверка подлинности живёт своим публичным адресом.
router = APIRouter(prefix="/courses")
me_router = APIRouter(prefix="/me")
verify_router = APIRouter(prefix="/verify")


@router.get("/{course_id}/completion")
def completion(
    course_id: int,
    user: Annotated[User | None, Depends(deps.get_current_user_optional)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> CompletionOut:
    # Публично, как и страница курса: вход только добавляет счётчики
    return CompletionOut(**svc.completion(course_id, user))


@router.post("/{course_id}/certificate")
def issue_certificate(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> CertificateOut:
    # Тела запроса нет; повторный вызов отдаёт 200 и тот же документ
    return CertificateOut(**svc.issue(user, course_id))


@me_router.get("/certificates")
def my_certificates(
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> MyCertificatesOut:
    return MyCertificatesOut(**svc.my_certificates(user))


@verify_router.get("/{number}")
def verify(
    number: str,
    request: Request,
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> VerifyOut:
    # Без входа: комиссия проверяет документ, не заводя аккаунта
    return VerifyOut(**svc.verify(number, deps.get_client_ip(request)))

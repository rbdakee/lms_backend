import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.adapters.db.models import User
from app.adapters.pdf.certificate import render_certificate
from app.api import deps
from app.api.schemas import CertificateStateOut, CompletionOut, MyCertificatesOut, VerifyOut
from app.application.certificate_pdf import CertificatePdfService
from app.application.certificates import CertificatesService

# Чек-лист и заявка висят на курсе, список — в кабинете учителя,
# проверка подлинности живёт своим публичным адресом, а бумага — своим:
# её открывает браузер, а не фронт. Выдачи здесь нет вовсе: она админская
# и лежит в admin_certificates.
router = APIRouter(prefix="/courses")
me_router = APIRouter(prefix="/me")
verify_router = APIRouter(prefix="/verify")
pdf_router = APIRouter(prefix="/certificates")


@router.get("/{course_id}/completion")
def completion(
    course_id: int,
    user: Annotated[User | None, Depends(deps.get_current_user_optional)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> CompletionOut:
    # Публично, как и страница курса: вход только добавляет счётчики
    return CompletionOut(**svc.completion(course_id, user, platform))


@router.post("/{course_id}/certificate")
def request_certificate(
    course_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> CertificateStateOut:
    # Ручка создаёт заявку, а документ по ней выписывает админ руками
    # (CERTIFICATES_BRIEF, 3). Тела запроса нет; повторный вызов отдаёт 200
    # и ту же строку — заявку или уже выданный документ
    return CertificateStateOut(**svc.request(user, course_id, platform))


@me_router.get("/certificates")
def my_certificates(
    user: Annotated[User, Depends(deps.get_current_user)],
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> MyCertificatesOut:
    return MyCertificatesOut(**svc.my_certificates(user, platform))


@verify_router.get("/{number}")
def verify(
    number: str,
    request: Request,
    platform: Annotated[str, Depends(deps.platform_of)],
    svc: Annotated[CertificatesService, Depends(deps.get_certificates_service)],
) -> VerifyOut:
    # Без входа: комиссия проверяет документ, не заводя аккаунта. Номер
    # с соседней площадки отвечает «не найден» — это её реестр
    return VerifyOut(**svc.verify(number, deps.get_client_ip(request), platform))


@pdf_router.get("/{certificate_id}/pdf")
def certificate_pdf(
    certificate_id: int,
    user: Annotated[User, Depends(deps.get_current_user)],
    svc: Annotated[CertificatePdfService, Depends(deps.get_certificate_pdf_service)],
) -> Response:
    # Сборка документа из данных сценария — это сериализация ответа, а не
    # логика: свести снимок и картинки в байты роутеру можно
    document, images, verify_url = svc.document(user, certificate_id)
    return Response(
        render_certificate(document, images, verify_url),
        media_type="application/pdf",
        headers={
            # Номер только из ASCII (алфавит без похожих начертаний),
            # поэтому filename* здесь не нужен. В кавычки уходят ровно те
            # знаки, из которых номер и состоит: инвариант живёт в другом
            # файле, и держать на нём разбор заголовка не стоит
            "content-disposition": f'attachment; filename="{_file_name(document["number"])}.pdf"'
        },
    )


def _file_name(number: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", number) or "certificate"

"""Сертификат бумагой: право на документ, снимок для печати и картинки
настроек.

Отдельным сценарием, а не внутри `certificates.py`: тому для чек-листа,
выдачи и публичной проверки не нужны ни настройки площадки, ни хранилище,
и дописывать их туда значит тащить в готовый файл чужие зависимости.

Данные — снимок из строки сертификата, без единого джойна на живые таблицы:
курс переименуют или удалят, учитель поправит ФИО — выданный документ обязан
остаться прежним (BACKEND_NOTES, раздел 6).
"""

from app.adapters.db.models import Certificate, User
from app.adapters.db.repos import CertificateRepo
from app.application.ports import StoragePort
from app.application.settings import CERT_SLOTS, SettingsService
from app.config import Settings
from app.domain.errors import ForbiddenError, NotFoundError

NOT_FOUND = "Сертификат не найден"


class CertificatePdfService:
    def __init__(
        self,
        certificates: CertificateRepo,
        settings: SettingsService,
        storage: StoragePort,
        cfg: Settings,
    ):
        self.certificates = certificates
        self.settings = settings
        self.storage = storage
        self.cfg = cfg

    # -- GET /certificates/{id}/pdf ----------------------------------------

    def document(
        self, user: User, certificate_id: int
    ) -> tuple[dict, dict[str, bytes | None], str | None]:
        """Снимок сертификата, байты трёх картинок и адрес проверки для QR.

        Порядок проверок общий, как в `files`: существование, потом право,
        — поэтому у чужого сертификата приходит 403, а не 404. Отозванный
        не отдаётся никому, включая владельца и админа: отозванная бумага
        не должна печататься заново.
        """
        certificate = self.certificates.by_id(certificate_id)
        if certificate is None:
            raise NotFoundError(NOT_FOUND)
        if certificate.user_id != user.id and not user.is_admin:
            raise ForbiddenError("Сертификат выдан другому человеку")
        if certificate.revoked_at is not None:
            raise NotFoundError(NOT_FOUND)
        return (
            {
                "holder_name": certificate.holder_name,
                "course_title": certificate.course_title,
                "hours": certificate.hours,
                "issued_at": certificate.issued_at,
                "number": certificate.number,
                # Язык документа — снимок момента выдачи, а не язык читателя
                "lang": certificate.lang,
            },
            {field: self._image(slot) for field, slot in CERT_SLOTS.items()},
            self._verify_url(certificate),
        )

    def _verify_url(self, certificate: Certificate) -> str | None:
        """Адрес страницы проверки этого документа — он уходит в QR.

        Берётся из конфигурации сервиса, а не из настроек площадки: страница
        `/verify` живёт в клиентском приложении, а не в API, и адрес её
        меняется при смене домена, а не при правке настроек админом.

        Пусто в конфигурации — QR не печатается вовсе: код, ведущий в никуда,
        хуже отсутствующего, потому что с выданной бумаги его не исправить.
        """
        base = self.cfg.verify_base_url.strip().rstrip("/")
        if not base:
            return None
        return f"{base}/verify/{certificate.number}"

    def _image(self, slot: str) -> bytes | None:
        """Байты картинки слота — или None, если её не ставили или объекта
        в хранилище уже нет.

        Отсутствие печати не повод отказать человеку в сертификате, поэтому
        пустой слот здесь не ошибка: место на бумаге просто останется пустым.
        """
        image = self.settings.image(slot)
        if image is None:
            return None
        if self.storage.size(image["key"]) is None:
            # Ключ в настройках есть, объекта нет: для документа это то же
            # самое, что не поставленная картинка
            return None
        return b"".join(self.storage.read(image["key"]))

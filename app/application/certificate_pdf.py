"""Сертификат бумагой: право на документ, снимок для печати и картинки
бренда.

Отдельным сценарием, а не внутри `certificates.py`: тому для чек-листа,
выдачи и публичной проверки не нужны ни бренд площадки, ни адрес проверки,
и дописывать их туда значит тащить в готовый файл чужие зависимости.

Данные — снимок из строки сертификата, без единого джойна на живые таблицы:
курс переименуют или удалят, учитель поправит ФИО — выданный документ обязан
остаться прежним (BACKEND_NOTES, раздел 6). Исключение ровно одно — ИИН,
и почему оно осознанное, сказано у `_holder_iin`.
"""

from app.adapters.db.models import Certificate, User
from app.adapters.db.repos import CertificateRepo, UserRepo
from app.application.settings import brand_image
from app.config import Settings
from app.domain.brands import CERT_SLOTS
from app.domain.errors import ForbiddenError, NotFoundError
from app.domain.iin import iin_filled

NOT_FOUND = "Сертификат не найден"


class CertificatePdfService:
    def __init__(self, certificates: CertificateRepo, users: UserRepo, cfg: Settings):
        self.certificates = certificates
        self.users = users
        self.cfg = cfg

    # -- GET /certificates/{id}/pdf ----------------------------------------

    def document(
        self, user: User, certificate_id: int
    ) -> tuple[dict, dict[str, bytes | None], str | None]:
        """Снимок сертификата, байты трёх картинок и адрес проверки для QR.

        Порядок проверок общий, как в `files`: существование, потом право,
        — поэтому у чужого сертификата приходит 403, а не 404. Отозванный
        не отдаётся никому, включая владельца и админа: отозванная бумага
        не должна печататься заново. Заявка не отдаётся по той же причине
        и тем же текстом: документа с номером и датой ещё нет, печатать
        нечего, а разного текста на «ещё нет» и «уже нет» посторонний
        видеть не должен.
        """
        certificate = self.certificates.by_id(certificate_id)
        if certificate is None:
            raise NotFoundError(NOT_FOUND)
        if certificate.user_id != user.id and not user.is_admin:
            raise ForbiddenError("Сертификат выдан другому человеку")
        if certificate.issued_at is None or certificate.revoked_at is not None:
            raise NotFoundError(NOT_FOUND)
        return (
            {
                "holder_name": certificate.holder_name,
                "course_title": certificate.course_title,
                "hours": certificate.hours,
                "issued_at": certificate.issued_at,
                "number": certificate.number,
                # Номер академии — тоже снимок: он вписан руками в момент
                # выдачи и у документов до 04.09.2026 пустой
                "registration_number": certificate.registration_number,
                "iin": self._holder_iin(certificate),
                # Язык документа — снимок момента выдачи, а не язык читателя
                "lang": certificate.lang,
            },
            # Картинки берутся у площадки самого документа, а не у площадки
            # запроса: бумагу открывает браузер прямой ссылкой, Origin в такой
            # запрос не приходит, и площадки у него нет вовсе
            {field: _image(certificate.platform, slot) for field, slot in CERT_SLOTS.items()},
            self._verify_url(certificate),
        )

    def _holder_iin(self, certificate: Certificate) -> str:
        """ИИН владельца — единственное поле листа, взятое из живой таблицы.

        Колонки `iin` у сертификата нет, и заводить её незачем: схема брифа
        её не заводит (CERTIFICATES_BRIEF, 5), номер у человека один на всю
        жизнь и после выдачи не меняется, а вторая копия самых чувствительных
        персональных данных, которые мы храним, — это две утечки вместо одной.

        Заглушка «не заполнен» на бумагу не идёт: на листе она читалась бы
        как настоящий номер из одних нулей. Владельца не нашлось (быть
        не должно — строку держит внешний ключ) — тоже пусто: лист выйдет
        без номера, а не откажет в печати, как и без картинки печати.
        """
        holder = self.users.by_id(certificate.user_id)
        if holder is None or not iin_filled(holder.iin):
            return ""
        return holder.iin

    def _verify_url(self, certificate: Certificate) -> str | None:
        """Адрес страницы проверки этого документа — он уходит в QR.

        Берётся у площадки самого документа, а не общий на все: проверка
        номера отвечает только по своей площадке (PLATFORMS_BRIEF, решение 10),
        и код второй площадки, уводящий на первую, честно показал бы там
        «не найдено». Ошибка эта видна не сразу — QR уже напечатан на бумаге.
        Площадку берём из строки сертификата по той же причине, что и картинки:
        бумагу открывает браузер прямой ссылкой, Origin в такой запрос
        не приходит, и площадки у него нет вовсе.

        Берётся из конфигурации сервиса, а не из настроек площадки: страница
        `/verify` живёт в клиентском приложении, а не в API, и адрес её
        меняется при смене домена, а не при правке настроек админом.

        Пусто в конфигурации — QR не печатается вовсе: код, ведущий в никуда,
        хуже отсутствующего, потому что с выданной бумаги его не исправить.
        """
        base = self.cfg.verify_base(certificate.platform).strip().rstrip("/")
        if not base:
            return None
        return f"{base}/verify/{certificate.number}"


def _image(platform: str, slot: str) -> bytes | None:
    """Байты картинки слота у площадки — или None, если файла у неё нет.

    Отсутствие печати не повод отказать человеку в сертификате, поэтому
    пустой слот здесь не ошибка: место на бумаге просто останется пустым.
    """
    path = brand_image(platform, slot)
    return path.read_bytes() if path is not None else None

"""Сертификаты в админке: очередь заявок, карточка, выдача, правка и отзыв.

Отдельно от `certificates.py` потому, что там человек просит документ себе
и смотрит на собственный чек-лист. Здесь на документ смотрят снаружи: кому
его выписать, каким номером академии и что делать с ошибочной строкой.
Условия курса тут заново не считаются — их проверили в момент заявки, и админ
выписывает бумагу по тому же чек-листу (CERTIFICATES_BRIEF, 3).

Здесь ходят персональные данные — ФИО и ИИН владельца документа. Уходят они
только админу, только по проверке на сервере и только в ответе списка
и карточки; в логи и в тексты ошибок ИИН не попадает ни при каких условиях
(CERTIFICATES_BRIEF, 1).
"""

from datetime import datetime

from app.adapters.db.models import Certificate, User
from app.adapters.db.repos import (
    CertificateAdminRepo,
    CertificateRepo,
    EnrollmentRepo,
    NotificationRepo,
    now_utc,
)
from app.domain.certificate import generate_number
from app.domain.errors import (
    CertificateIssuedError,
    CertificateNotIssuedError,
    ConflictError,
    FieldError,
    IinRequiredError,
    NotFoundError,
)
from app.domain.iin import iin_filled

# Случайная часть номера — 32 в шестой степени, около миллиарда вариантов:
# занятый номер это невероятное совпадение, и хватает попробовать ещё раз.
NUMBER_TRIES = 5


def _status(certificate: Certificate) -> str:
    """Три состояния одной и той же строки: заявка, документ, отозванный.

    Отдельной таблицы заявок нет (CERTIFICATES_BRIEF, 5), поэтому состояние
    складывается из двух дат. Отзыв читается первым: отозванная заявка
    остаётся отозванной, а не возвращается в очередь на выдачу.
    """
    if certificate.revoked_at is not None:
        return "revoked"
    return "issued" if certificate.issued_at is not None else "requested"


class CertificatesAdminService:
    def __init__(
        self,
        certificates: CertificateRepo,
        admin_certificates: CertificateAdminRepo,
        notifications: NotificationRepo,
        enrollments: EnrollmentRepo,
    ):
        self.certificates = certificates
        self.admin_certificates = admin_certificates
        self.notifications = notifications
        # Отметка «курс пройден» ставится выдачей: раньше её ставил учительский
        # сценарий, а теперь курс считается пройденным, когда админ выписал
        # документ
        self.enrollments = enrollments

    # -- GET /admin/certificates -----------------------------------------

    def admin_list(
        self,
        *,
        status: str | None,
        platform: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> dict:
        rows, total = self.admin_certificates.page(
            status=status, platform=platform, q=q, offset=offset, limit=limit
        )
        return {
            "items": [
                self._certificate_out(certificate, teacher)
                for certificate, teacher in rows
            ],
            "total": total,
        }

    # -- GET /admin/certificates/{id} ------------------------------------

    def card(self, certificate_id: int) -> dict:
        # Предупреждения на открытии нет: оно отвечает на то, что админ только
        # что ввёл, а не висит у документа постоянной меткой
        return self._card_out(*self._found(certificate_id))

    # -- POST /admin/certificates/{id}/issue -----------------------------

    def issue(self, admin: User, certificate_id: int, registration_number: str) -> dict:
        """Выдача: наш номер, дата и регистрационный номер академии появляются
        одним движением.

        Отозвать выданный документ «уже неловко» (backend/CLAUDE.md), поэтому
        порядок проверок здесь и есть вся защита: сначала выясняем, что перед
        нами заявка и выписать её есть кому, и только потом что-то пишем.
        """
        certificate, teacher = self._found(certificate_id)
        if certificate.revoked_at is not None:
            # Отозванная строка остаётся в реестре отозванной: вторая бумага
            # по ней была бы документом с чужой историей. Нужен новый —
            # учитель просит его заново, отзыв это как раз и открывает
            raise ConflictError("Сертификат отозван — выдать его снова нельзя")
        if certificate.issued_at is not None:
            raise CertificateIssuedError("Сертификат уже выдан — правьте его через «Сохранить»")
        registration_number = registration_number.strip()
        if not registration_number:
            raise FieldError("registration_number", "Регистрационный номер обязателен")
        if not iin_filled(teacher.iin):
            # ИИН спрашивается второй раз после заявки нарочно: между заявкой
            # и выдачей проходит время, а без номера человека не внести
            # в реестр академии. Самого номера в тексте отказа нет и быть
            # не может
            raise IinRequiredError("У учителя не заполнен ИИН — сертификат выдать нельзя")

        issued_at = now_utc()
        self._take_number(certificate, issued_at)
        certificate.registration_number = registration_number
        # Кто выдал — по правилу «у действия админа хранится actor_id»
        # (backend/CLAUDE.md); наружу поле не отдаётся, экрана под него нет
        certificate.issued_by = admin.id

        # Текст уведомления не храним — соберётся на языке читателя.
        # Площадка — у документа: ссылка из колокольчика ведёт на тот сайт,
        # где сертификат получен
        self.notifications.create(
            certificate.user_id,
            "certificate_issued",
            {
                "course_id": certificate.course_id,
                "course_title": certificate.course_title,
                "certificate_id": certificate.id,
            },
            certificate.platform,
        )
        enrollment = self.enrollments.active_for(
            certificate.user_id, certificate.course_id, certificate.platform
        )
        if enrollment is not None and enrollment.completed_at is None:
            # «Курс пройден» и «сертификат выдан» — одно событие: вкладка
            # «Пройденные» на /my держится именно на нём. Доступ мог быть
            # отозван — тогда отмечать нечего, а документ всё равно выдаётся
            enrollment.completed_at = issued_at
        return self._card_out(certificate, teacher, self._warning(certificate))

    def _take_number(self, certificate: Certificate, issued_at: datetime) -> None:
        """Занять свободный номер за документом.

        Попытками, а не проверкой «есть ли такой»: между проверкой и записью
        успела бы вклиниться соседняя выдача, а уникальность держит база.
        Год номера берётся от даты выдачи — номер и дата на бумаге не должны
        разойтись.
        """
        for _ in range(NUMBER_TRIES):
            if self.certificates.take_number(
                certificate, generate_number(issued_at), issued_at
            ):
                return
        raise RuntimeError("Свободный номер сертификата не подобрался")

    # -- PATCH /admin/certificates/{id} ----------------------------------

    def patch(self, certificate_id: int, fields: dict) -> dict:
        """Правятся снимки на бумаге и то, что админ вписал руками.

        `number`, `user_id`, `course_id` и `platform` сюда не приходят вовсе —
        их отбивает `extra: forbid` у схемы: смена любого означает другой
        документ, а не правку этого (CERTIFICATES_BRIEF, 4).
        """
        certificate, teacher = self._found(certificate_id)
        if "registration_number" in fields:
            # Пустая строка допускается: так админ стирает ошибочный номер
            certificate.registration_number = (fields["registration_number"] or "").strip()
        if "holder_name" in fields:
            holder_name = (fields["holder_name"] or "").strip()
            if not holder_name:
                # Имя печатается на бумаге: документ без владельца печатать
                # некому и незачем
                raise FieldError("holder_name", "ФИО обязательно — оно печатается на сертификате")
            certificate.holder_name = holder_name
        if "course_title" in fields:
            course_title = (fields["course_title"] or "").strip()
            if not course_title:
                raise FieldError("course_title", "Название курса обязательно")
            certificate.course_title = course_title
        # Присланный null у часов и языка — «не трогать», а не «стереть»:
        # в базе они обязательные, и снимать их нечем
        if fields.get("hours") is not None:
            certificate.hours = fields["hours"]
        if fields.get("lang") is not None:
            certificate.lang = fields["lang"]
        if "issued_at" in fields:
            certificate.issued_at = self._issued_at(certificate, fields["issued_at"])
        return self._card_out(certificate, teacher, self._warning(certificate))

    @staticmethod
    def _issued_at(certificate: Certificate, value: datetime | None) -> datetime:
        """Дату выдачи можно поправить, но не завести и не стереть: её ставит
        выдача, а снимает отзыв."""
        if certificate.issued_at is None:
            raise CertificateNotIssuedError("Сертификат ещё не выдан — дату выдачи ставит выдача")
        if value is None:
            # Стёртая дата отменила бы выдачу в обход отзыва: строка стала бы
            # заявкой, а наш номер остался бы за ней
            raise FieldError("issued_at", "Дату выдачи нельзя стереть")
        return value

    # -- POST /admin/certificates/{id}/revoke ----------------------------

    def revoke(self, admin: User, certificate_id: int) -> dict:
        """Отзыв работает и над выданным документом, и над заявкой: снять
        ошибочную заявку больше нечем, а отозванная строка освобождает
        `uq_certificate_active` — и человек может попросить сертификат снова.

        Уведомления учителю нет: типа `certificate_revoked` в
        `domain/notifications.py` не существует, а заводить текст, которого
        не просили, в эту сессию не входит.
        """
        certificate, teacher = self._found(certificate_id)
        if certificate.revoked_at is None:
            certificate.revoked_at = now_utc()
            certificate.revoked_by = admin.id
        # Повторный отзыв ничего не меняет: время остаётся временем первого
        # отзыва — так же устроено закрытие доступа к курсу
        return self._card_out(certificate, teacher)

    # -- сборка ответа ---------------------------------------------------

    def _found(self, certificate_id: int) -> tuple[Certificate, User]:
        found = self.admin_certificates.by_id(certificate_id)
        if found is None:
            raise NotFoundError("Сертификат не найден")
        return found

    def _card_out(
        self,
        certificate: Certificate,
        teacher: User,
        warning: dict | None = None,
    ) -> dict:
        """Карточка и ответ каждого действия — одной формы: экран
        перерисовывается одним и тем же куском кода."""
        return {
            "certificate": self._certificate_out(certificate, teacher),
            "warning": warning,
        }

    def _warning(self, certificate: Certificate) -> dict | None:
        """Такой номер академии уже стоит у другого документа.

        Предупреждение, а не отказ: чужой нумерации мы не знаем, а запрет
        остановил бы админа посреди работы (CERTIFICATES_BRIEF, 2). Сохранение
        к этому моменту уже произошло — предупреждение уходит вместе с новой
        карточкой.
        """
        other = self.admin_certificates.same_registration_number(
            certificate.registration_number, certificate.id
        )
        if other is None:
            return None
        return {
            "code": "registration_number_taken",
            # По нашему номеру админ тот документ и найдёт; у невыданного
            # номера ещё нет, и остаётся показать id
            "message": f"Такой номер уже есть у сертификата №{other.number or other.id}",
            "certificate_id": other.id,
            "number": other.number,
        }

    @staticmethod
    def _certificate_out(certificate: Certificate, teacher: User) -> dict:
        """Строка списка и карточка — одна и та же форма: полей у документа
        немного, и второй под карточку заводить незачем.

        Всё, что описывает документ, берётся из его собственных снимков:
        курс переименуют или сократят часы — выданная бумага от этого
        не меняется. Живого курса здесь нет вовсе — из него не берётся
        даже название.

        Учитель, наоборот, нужен живой: ФИО и ИИН админ сверяет с профилем
        перед выдачей, а в снимке ИИН нет и не будет.
        """
        return {
            "id": certificate.id,
            "status": _status(certificate),
            # Метка площадки: админка одна на обе, и в строке видно, откуда
            # документ. Кодом, а не именем — имя админка возьмёт
            # из справочника GET /admin/settings
            "platform": certificate.platform,
            "number": certificate.number,
            "registration_number": certificate.registration_number,
            "holder_name": certificate.holder_name,
            "course_id": certificate.course_id,
            "course_title": certificate.course_title,
            "hours": certificate.hours,
            "lang": certificate.lang,
            "requested_at": certificate.requested_at,
            "issued_at": certificate.issued_at,
            "revoked_at": certificate.revoked_at,
            "teacher": {
                "id": teacher.id,
                "last_name": teacher.last_name,
                "first_name": teacher.first_name,
                "middle_name": teacher.middle_name,
                # ИИН уходит только здесь и в карточке учителя, и только
                # админу: по нему он сверяет человека перед выдачей
                "iin": teacher.iin,
            },
        }

"""Колокольчик учителя: страница уведомлений и отметка «прочитано»."""

from app.adapters.db.models import Notification, User
from app.adapters.db.repos import NotificationRepo
from app.domain.errors import FieldError
from app.domain.notifications import notification_text


class NotificationsService:
    def __init__(self, notifications: NotificationRepo):
        self.notifications = notifications

    # -- GET /notifications ---------------------------------------------

    def page(self, user: User, offset: int, limit: int, platform: str) -> dict:
        rows = self.notifications.page(user.id, offset, limit, platform)
        return {
            "items": [self._out(notification, user.lang) for notification in rows],
            # Счётчик идёт сверх страницы: он нужен шапке на каждом экране,
            # и панель колокольчика берёт его тем же запросом (CONTRACT, сессия 6).
            # Площадка у всех трёх чисел одна — своя: иначе на колокольчике
            # горело бы число, которого нет в списке
            "unread_count": self.notifications.unread_count(user.id, platform),
            "total": self.notifications.count(user.id, platform),
        }

    # -- POST /notifications/read ---------------------------------------

    def mark_read(
        self, user: User, ids: list[int] | None, all_: bool | None, platform: str
    ) -> None:
        if (ids is None) == (all_ is None):
            raise FieldError("ids", "Нужно указать ids или all")
        if all_ is False:
            # Поле прислано, но отмечать нечего — это не ошибка
            return
        # all=true — ids здесь None, и репозиторий понимает это как «все свои»
        # на этой площадке: соседний список гасится со своего домена
        self.notifications.mark_read(user.id, ids, platform)

    # -- сборка ответа ---------------------------------------------------

    @staticmethod
    def _out(notification: Notification, lang: str) -> dict:
        return {
            "id": notification.id,
            "type": notification.type,
            # Текст собирается сейчас и по языку читателя: в базе его нет
            # (BACKEND_NOTES, раздел 11)
            "text": notification_text(notification.type, notification.params, lang),
            # params уходят рядом с текстом — из них фронт строит адрес перехода
            "params": notification.params,
            "read_at": notification.read_at,
            "created_at": notification.created_at,
        }

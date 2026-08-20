import logging

from app.application.ports import NotificationCard

log = logging.getLogger("telegram")


class LogTelegram:
    """Заглушка до настоящего бота: пишет уведомление в лог.

    Карточка по правилам ПД не содержит ФИО и телефонов — только номер
    заявки, курс и ссылку в админку, поэтому в лог она попадает целиком.
    """

    def notify_admins(self, card: NotificationCard) -> None:
        details = " · ".join(card.lines)
        log.info("Telegram админам: %s — %s (%s)", card.title, details, card.link_url)

    def send_to(self, chat_id: str, text: str) -> None:
        # Ответ боту тоже без ПД: подтверждение привязки и отказ по коду.
        # Идентификатор чата в лог не идёт: бот публичный, и любой написавший
        # ему `/start` оставил бы там свой Telegram-идентификатор
        log.info("Telegram в чат: %s", text)

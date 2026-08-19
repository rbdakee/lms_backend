import logging

log = logging.getLogger("telegram")


class LogTelegram:
    """Заглушка до настоящего бота: пишет уведомление в лог.

    Текст уведомления по правилам ПД не содержит ФИО и телефонов —
    только номер заявки и курс, поэтому в лог он попадает целиком.
    """

    def notify_admins(self, text: str) -> None:
        log.info("Telegram админам: %s", text)

    def send_to(self, chat_id: str, text: str) -> None:
        # Ответ боту тоже без ПД: подтверждение привязки и отказ по коду.
        # Идентификатор чата в лог не идёт: бот публичный, и любой написавший
        # ему `/start` оставил бы там свой Telegram-идентификатор
        log.info("Telegram в чат: %s", text)

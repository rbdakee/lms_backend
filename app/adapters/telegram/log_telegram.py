import logging

log = logging.getLogger("telegram")


class LogTelegram:
    """Заглушка до настоящего бота: пишет уведомление в лог.

    Текст уведомления по правилам ПД не содержит ФИО и телефонов —
    только номер заявки и курс, поэтому в лог он попадает целиком.
    """

    def notify_admins(self, text: str) -> None:
        log.info("Telegram админам: %s", text)

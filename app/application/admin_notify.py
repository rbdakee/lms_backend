"""Уведомления админам: порт Telegram плюс флаги типов сообщений.

Флаги `notify_leads` и `notify_submissions` — настройка площадки, поэтому
решение «слать или нет» живёт здесь, а не в адаптере: иначе каждый следующий
адаптер переписывал бы эту проверку заново, а переключатель на экране врал бы
до тех пор, пока его кто-нибудь не забудет.

Доставка остаётся тем же, чем была: Telegram — не хранилище, заявка пишется
в базу до отправки, и выключенный флаг ничего в админке не отменяет.
"""

from app.adapters.db.repos import SettingRepo
from app.application.ports import TelegramPort
from app.application.settings import TELEGRAM_KEY

# Флаг настройки на каждый тип сообщения — их ровно два (BACKEND_NOTES, 11)
NOTIFY_FLAGS = {"lead": "notify_leads", "submission": "notify_submissions"}


class AdminNotifier:
    def __init__(self, telegram: TelegramPort, settings: SettingRepo):
        self.telegram = telegram
        self.settings = settings

    def notify_admins(self, text: str, *, kind: str) -> None:
        stored = self.settings.get(TELEGRAM_KEY)
        # По умолчанию включены: бот привязывают затем, чтобы получать заявки
        # и работы на проверку
        if not stored.get(NOTIFY_FLAGS[kind], True):
            return
        self.telegram.notify_admins(text)

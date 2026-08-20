"""Уведомления админам: порт Telegram плюс флаги типов сообщений.

Флаги `notify_leads` и `notify_submissions` — настройка площадки, поэтому
решение «слать или нет» живёт здесь, а не в адаптере: иначе каждый следующий
адаптер переписывал бы эту проверку заново, а переключатель на экране врал бы
до тех пор, пока его кто-нибудь не забудет.

Доставка остаётся тем же, чем была: Telegram — не хранилище, заявка пишется
в базу до отправки, и выключенный флаг ничего в админке не отменяет.
"""

from collections.abc import Callable

from app.adapters.db.repos import SettingRepo
from app.application.ports import NotificationCard, TelegramPort
from app.application.settings import TELEGRAM_KEY

# Флаг настройки на каждый тип сообщения — их ровно два (BACKEND_NOTES, 11)
NOTIFY_FLAGS = {"lead": "notify_leads", "submission": "notify_submissions"}


class AdminNotifier:
    def __init__(
        self, telegram: TelegramPort, settings: SettingRepo, commit: Callable[[], None]
    ):
        self.telegram = telegram
        self.settings = settings
        # Соединение с базой отпускается до похода в Telegram: отправка идёт
        # с повторами и в худшем случае занимает шестнадцать секунд, а держать
        # столько соединение из пятнадцати — значит уронить всю площадку
        # на чужой аварии. Читать флаг после отправки нельзя: тогда решение
        # «слать или нет» опоздает.
        self.commit = commit

    def notify_admins(self, card: NotificationCard, *, kind: str) -> None:
        stored = self.settings.get(TELEGRAM_KEY)
        # По умолчанию включены: бот привязывают затем, чтобы получать заявки
        # и работы на проверку
        enabled = stored.get(NOTIFY_FLAGS[kind], True)
        # Чтение флага открыло свою транзакцию — закрываем её здесь, а не
        # в конце запроса: дальше идут секунды сетевого ожидания
        self.commit()
        if not enabled:
            return
        self.telegram.notify_admins(card)

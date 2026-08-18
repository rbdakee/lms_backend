"""Казахстанское время (BACKEND_NOTES, раздел 7): границы суток считаем
по Алматы, а не по UTC — в 01:00 по Казахстану заявка, созданная час назад,
не должна выглядеть вчерашней.
"""

from datetime import datetime, timedelta, timezone

# С 2024 года вся страна в едином поясе UTC+5 без сезонных переводов, поэтому
# фиксированное смещение, а не tzdata: в минимальном образе Python базы
# таймзон может не быть, а новые зависимости запрещены.
ALMATY = timezone(timedelta(hours=5))


def waiting_days(since: datetime, now: datetime) -> int:
    """Целые дни ожидания: обе точки переводим в Алматы, берём даты, вычитаем."""
    return (now.astimezone(ALMATY).date() - since.astimezone(ALMATY).date()).days

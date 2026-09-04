from app.domain.iin import iin_filled


def names_filled(first_name: str, last_name: str) -> bool:
    """Фамилия и имя заполнены: экран онбординга требует только их,
    остальное можно дозаполнить в профиле."""
    return bool(first_name.strip() and last_name.strip())


def onboarding_done(first_name: str, last_name: str, iin: str, *, is_admin: bool) -> bool:
    """С 04.09.2026 мимо онбординга не проходит и тот, у кого нулевой ИИН:
    спрашивать номер в момент выдачи сертификата поздно — админ упрётся
    в пустое поле тогда, когда учитель уже всё сдал и ждёт документ
    (CERTIFICATES_BRIEF, 1).

    Админу ИИН не нужен: он не учится и сертификатов не получает, а требовать
    с него номер — держать чужие персональные данные без причины.
    """
    return names_filled(first_name, last_name) and (is_admin or iin_filled(iin))

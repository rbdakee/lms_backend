"""Первый админ на чистой базе.

Прод поднимается с пустой базой: пользователей нет, а значит нет и того,
кто выдаст первый доступ, заведёт курс и увидит заявки. Заводить его руками
в базе — значит держать в инструкции по деплою `INSERT` с телефоном; вместо
этого пара `AUTH_BOOTSTRAP_PHONE` + `AUTH_BOOTSTRAP_CODE` заводит его при
старте и она же пускает его без SMS (`app/application/auth.py`, `_code_for`).

Это бэкдор, и он назван бэкдором: пока настоящего SMS-провайдера нет, один
номер входит известным кодом. Снимается снятием двух переменных, без релиза;
`is_admin` у заведённого пользователя при этом остаётся — пропадает вход
без SMS, а не сам админ.
"""

import logging

from app.adapters.db.repos import UserRepo
from app.config import Settings
from app.domain.phone import normalize_phone

log = logging.getLogger(__name__)


def ensure_bootstrap_admin(users: UserRepo, cfg: Settings, commit) -> None:
    """Идемпотентно: при каждом старте либо заводит номер админом, либо
    убеждается, что он им остался. Номер в лог не пишем — это персональные
    данные и половина входа сразу."""
    if not cfg.auth_bootstrap_phone:
        return
    phone = normalize_phone(cfg.auth_bootstrap_phone)
    user = users.by_phone(phone)
    if user is None:
        user = users.create(phone)
    if not user.is_admin:
        user.is_admin = True
    commit()
    log.warning(
        "AUTH_BOOTSTRAP_PHONE: один номер входит фиксированным кодом без SMS"
        " и заведён админом. Снять — убрать AUTH_BOOTSTRAP_PHONE"
        " и AUTH_BOOTSTRAP_CODE из окружения."
    )

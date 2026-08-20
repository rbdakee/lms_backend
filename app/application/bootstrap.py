"""Первый админ на чистой базе.

Прод поднимается с пустой базой: пользователей нет, а значит нет и того,
кто выдаст первый доступ, заведёт курс и увидит заявки. Заводить его руками
в базе — значит держать в инструкции по деплою `INSERT` с телефоном; вместо
этого пара `AUTH_BOOTSTRAP_PHONE` + `AUTH_BOOTSTRAP_CODE` заводит его при
старте и она же пускает его без SMS (`app/application/auth.py`, `_code_for`).

Половины две, и они не равны. `AUTH_BOOTSTRAP_PHONE` — обычная боевая
настройка: заводит админа, входит он кодом от провайдера. А вот
`AUTH_BOOTSTRAP_CODE` — бэкдор, и назван он бэкдором: тот же номер входит
известным кодом, минуя провайдера. Нужен он ровно там, где кода взять
неоткуда (`SMS_PROVIDER=log`), и снимается снятием переменной, без релиза;
`is_admin` у заведённого пользователя при этом остаётся.
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
    if cfg.auth_bootstrap_code:
        log.warning(
            "AUTH_BOOTSTRAP_CODE: один номер входит фиксированным кодом,"
            " минуя провайдера. Снять — убрать переменную из окружения."
        )
    else:
        log.info("AUTH_BOOTSTRAP_PHONE: номер заведён админом")

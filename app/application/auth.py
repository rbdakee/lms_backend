"""Вход по SMS-коду. Правила — с экрана /login и из раздела 8 BACKEND_NOTES:
код 4 цифры на 5 минут, 3 попытки ввода, блок на 10 минут, повтор через
минуту, суточные потолки на номер и на IP.
"""

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from app.adapters.db.models import Session, User
from app.adapters.db.repos import AuthCodeRepo, SessionRepo, UserRepo, now_utc
from app.application.ports import SmsPort
from app.config import Settings
from app.domain.errors import (
    BlockedError,
    CodeExpiredError,
    RateLimitedError,
    TooManyAttemptsError,
    ValidationAppError,
    WrongCodeError,
)
from app.domain.phone import normalize_phone


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_code(phone: str, code: str) -> str:
    # Соль номером: одинаковые коды у разных номеров дают разные хэши
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def _seconds_until(moment: datetime) -> int:
    return max(1, int((moment - now_utc()).total_seconds()))


class AuthService:
    def __init__(
        self,
        users: UserRepo,
        sessions: SessionRepo,
        codes: AuthCodeRepo,
        sms: SmsPort,
        cfg: Settings,
        commit: Callable[[], None],
    ):
        self.users = users
        self.sessions = sessions
        self.codes = codes
        self.sms = sms
        self.cfg = cfg
        # Коммит нужен сценарию точечно: счётчик попыток обязан сохраниться
        # даже когда сценарий завершается ошибкой «неверный код».
        self.commit = commit

    def _normalize(self, raw_phone: str) -> str:
        phone = normalize_phone(raw_phone)
        if phone is None:
            raise ValidationAppError("Проверьте номер: нужен казахстанский, 10 цифр после +7")
        return phone

    def _check_not_blocked(self, phone: str) -> None:
        until = self.codes.blocked_until(phone)
        if until is not None:
            raise TooManyAttemptsError(_seconds_until(until))

    def request_code(self, raw_phone: str, consent: bool, ip: str | None) -> int:
        phone = self._normalize(raw_phone)
        if not consent:
            raise ValidationAppError("Нужно согласие на обработку персональных данных")

        user = self.users.by_phone(phone)
        if user is not None and user.is_blocked:
            raise BlockedError()

        self._check_not_blocked(phone)

        # Дальше идут одни «прочитать, посчитать, записать»: и повтор раз
        # в минуту, и оба суточных потолка. Без блокировки одновременные
        # запросы читают один и тот же счётчик и уходят каждый своей SMS —
        # почему именно советующая блокировка, написано у lock_sending
        self.codes.lock_sending(phone, ip)

        last = self.codes.last_sent_at(phone)
        if last is not None:
            since = (now_utc() - last).total_seconds()
            if since < self.cfg.code_resend_sec:
                wait = int(self.cfg.code_resend_sec - since) or 1
                raise RateLimitedError(
                    "Код уже отправлен — новый можно запросить через минуту",
                    retry_after_sec=wait,
                )

        # SMS стоит денег: суточные потолки против цикла в три строки
        for kind, count, limit in (
            ("phone", self.codes.count_in_day(phone=phone), self.cfg.phone_codes_per_day),
            ("ip", self.codes.count_in_day(ip=ip) if ip else 0, self.cfg.ip_codes_per_day),
        ):
            if count >= limit:
                oldest = self.codes.oldest_in_day(
                    phone=phone if kind == "phone" else None,
                    ip=ip if kind == "ip" else None,
                )
                wait = _seconds_until(oldest + timedelta(hours=24)) if oldest else 3600
                raise RateLimitedError(
                    "Лимит SMS на сегодня исчерпан — попробуйте позже",
                    retry_after_sec=wait,
                )

        self.codes.expire_active(phone)
        code = self._code_for(phone)
        self.codes.create(phone, _hash_code(phone, code), ip, self.cfg.code_ttl_min)
        # Коммит до отправки, а не после: он отпускает блокировки lock_sending,
        # которые иначе держались бы весь поход к SMS-шлюзу. Ключ адреса
        # за прокси один на всех, и вход всей площадки встал бы в очередь
        # со скоростью одной SMS за раз.
        #
        # Плата за это известна: не ушедшая SMS оставляет строку кода
        # в базе, и человек ждёт минуту до повторной отправки. Обратный
        # порядок стоил бы дороже — очередь на входе для всех сразу.
        self.commit()
        self.sms.send_code(phone, code)
        return self.cfg.code_resend_sec

    def _code_for(self, phone: str) -> str:
        """Код входа: обычно случайный, для одного номера — фиксированный
        из окружения.

        Настоящего SMS-провайдера нет, заглушка печатает код в лог сервиса,
        и без этой пары в бою войти может только тот, кто читает логи.
        Всё остальное с фиксированным кодом происходит как с любым другим:
        он живёт пять минут, гасится входом, три неверных ввода блокируют
        номер на десять минут. Пара проверена при старте
        (`check_bootstrap_login`) и снимается снятием двух переменных.
        """
        bootstrap = self.cfg.auth_bootstrap_phone
        if bootstrap and phone == normalize_phone(bootstrap):
            return self.cfg.auth_bootstrap_code
        return "".join(secrets.choice("0123456789") for _ in range(self.cfg.code_length))

    def verify_code(
        self, raw_phone: str, code: str, user_agent: str, ip: str | None
    ) -> tuple[User, str]:
        phone = self._normalize(raw_phone)
        self._check_not_blocked(phone)

        active = self.codes.active_for_phone(phone)
        if active is None or active.expires_at <= now_utc():
            raise CodeExpiredError()

        # Строка кода занимается до сверки: попытки считаются, чтобы подбор
        # четырёхзначного кода стоил три сверки, а не три сверки на залп
        attempts = self.codes.lock_attempts(active)
        if attempts >= self.cfg.code_max_attempts:
            # Попытки исчерпал сосед по залпу, пока мы ждали строку: сверять
            # уже нечего, и ответ у нас с ним один
            raise TooManyAttemptsError(self.cfg.code_block_min * 60)

        if _hash_code(phone, code) != active.code_hash:
            attempts_left = self.cfg.code_max_attempts - self.codes.bump_attempts(active)
            if attempts_left <= 0:
                active.blocked_until = now_utc() + timedelta(minutes=self.cfg.code_block_min)
                self.commit()
                raise TooManyAttemptsError(self.cfg.code_block_min * 60)
            self.commit()
            raise WrongCodeError(attempts_left)

        active.used_at = now_utc()

        user = self.users.by_phone(phone)
        if user is None:
            user = self.users.create(phone)
        if user.is_blocked:
            # Код погашен, но сессии заблокированному не будет
            self.commit()
            raise BlockedError()

        token = secrets.token_urlsafe(32)
        self.sessions.create(user.id, hash_token(token), user_agent, ip)
        return user, token

    def logout(self, token: str) -> None:
        session = self.sessions.by_token_hash(hash_token(token))
        if session is not None:
            self.sessions.revoke(session)

    def logout_others(self, current: Session) -> int:
        return self.sessions.revoke_others(current.user_id, current.id)

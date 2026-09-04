import uuid

from app.adapters.db.models import Session, User
from app.adapters.db.repos import SessionRepo, UserRepo
from app.domain.errors import FieldError, IinTakenError, NotFoundError
from app.domain.iin import normalize_iin


class UsersService:
    def __init__(self, sessions: SessionRepo, users: UserRepo):
        self.sessions = sessions
        self.users = users

    def update_profile(self, user: User, fields: dict) -> User:
        # ИИН разбирается до общего цикла: он проверяется, сверяется с чужими
        # аккаунтами и не стирается — общие правила ниже ему не подходят
        if "iin" in fields:
            self._set_iin(user, fields["iin"])
        # fields уже отвалидированы схемой; сюда попадает только присланное
        for name, value in fields.items():
            if name == "iin":
                continue
            if isinstance(value, str):
                value = value.strip()
            elif value is None and name not in ("experience", "lang"):
                # null для строкового поля — «стереть»; язык не сбрасывается
                value = ""
            if name == "lang" and value is None:
                continue
            setattr(user, name, value)
        return user

    def _set_iin(self, user: User, raw: str | None) -> None:
        """ИИН нужен академии, чтобы внести человека в реестр
        (CERTIFICATES_BRIEF, 1), поэтому снять его нельзя — иначе админ
        упрётся в пустое поле тогда, когда учитель уже всё сдал и ждёт
        документ.

        Ни присланного номера, ни аккаунта, где он уже стоит, отсюда наружу
        не уходит: ИИН — персональные данные, и в текст ошибки он не попадает.

        Гонку двух одновременных сохранений ловит частичный уникальный индекс
        в базе; проверкой в коде она не дублируется.
        """
        if raw is None:
            # null — «не трогать», а не «стереть»: так же ведёт себя lang
            return
        iin = normalize_iin(raw)
        if iin is None:
            raise FieldError("iin", "ИИН — 12 цифр")
        existing = self.users.by_iin(iin)
        # Свой же номер занятым не считается: человек сохранил профиль
        # второй раз, не тронув поле
        if existing is not None and existing.id != user.id:
            raise IinTakenError()
        user.iin = iin

    def list_sessions(self, user: User, current: Session) -> list[dict]:
        return [
            {
                "id": str(s.id),
                "created_at": s.created_at,
                "last_seen_at": s.last_seen_at,
                "user_agent": s.user_agent,
                "is_current": s.id == current.id,
            }
            for s in self.sessions.list_for_user(user.id)
        ]

    def revoke_session(self, user: User, session_id: uuid.UUID) -> None:
        session = self.sessions.by_id_for_user(session_id, user.id)
        if session is None:
            raise NotFoundError("Такой сессии нет")
        self.sessions.revoke(session)

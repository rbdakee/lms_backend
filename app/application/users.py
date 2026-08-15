import uuid

from app.adapters.db.models import Session, User
from app.adapters.db.repos import SessionRepo
from app.domain.errors import NotFoundError


class UsersService:
    def __init__(self, sessions: SessionRepo):
        self.sessions = sessions

    def update_profile(self, user: User, fields: dict) -> User:
        # fields уже отвалидированы схемой; сюда попадает только присланное
        for name, value in fields.items():
            if isinstance(value, str):
                value = value.strip()
            elif value is None and name not in ("experience", "lang"):
                # null для строкового поля — «стереть»; язык не сбрасывается
                value = ""
            if name == "lang" and value is None:
                continue
            setattr(user, name, value)
        return user

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

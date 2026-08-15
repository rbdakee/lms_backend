"""Репозитории — адаптер Postgres. Сценарии получают их готовыми объектами
и не знают про SQLAlchemy."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import AuthCode, Session, User


def now_utc() -> datetime:
    return datetime.now(UTC)


class UserRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def by_phone(self, phone: str) -> User | None:
        return self.db.scalar(select(User).where(User.phone == phone))

    def by_id(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def create(self, phone: str) -> User:
        # Согласие проставляется при создании: без галочки код не отправляется
        user = User(phone=phone, consented_at=now_utc())
        self.db.add(user)
        self.db.flush()
        return user


class AuthCodeRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def active_for_phone(self, phone: str) -> AuthCode | None:
        """Последний неиспользованный код номера; действует всегда последний."""
        return self.db.scalar(
            select(AuthCode)
            .where(AuthCode.phone == phone, AuthCode.used_at.is_(None))
            .order_by(AuthCode.created_at.desc())
            .limit(1)
        )

    def blocked_until(self, phone: str) -> datetime | None:
        until = self.db.scalar(
            select(func.max(AuthCode.blocked_until)).where(AuthCode.phone == phone)
        )
        if until is not None and until > now_utc():
            return until
        return None

    def last_sent_at(self, phone: str) -> datetime | None:
        return self.db.scalar(
            select(func.max(AuthCode.created_at)).where(AuthCode.phone == phone)
        )

    def oldest_in_day(self, *, phone: str | None = None, ip: str | None = None) -> datetime | None:
        """Начало суточного окна — чтобы честно сказать, когда лимит отпустит."""
        q = select(func.min(AuthCode.created_at)).where(
            AuthCode.created_at > now_utc() - timedelta(hours=24)
        )
        q = q.where(AuthCode.phone == phone) if phone else q.where(AuthCode.ip == ip)
        return self.db.scalar(q)

    def count_in_day(self, *, phone: str | None = None, ip: str | None = None) -> int:
        q = select(func.count()).select_from(AuthCode).where(
            AuthCode.created_at > now_utc() - timedelta(hours=24)
        )
        q = q.where(AuthCode.phone == phone) if phone else q.where(AuthCode.ip == ip)
        return self.db.scalar(q) or 0

    def expire_active(self, phone: str) -> None:
        # Новый код гасит предыдущие: действителен всегда последний отправленный
        self.db.execute(
            update(AuthCode)
            .where(AuthCode.phone == phone, AuthCode.used_at.is_(None))
            .values(expires_at=now_utc())
        )

    def create(self, phone: str, code_hash: str, ip: str | None, ttl_min: int) -> AuthCode:
        code = AuthCode(
            phone=phone,
            code_hash=code_hash,
            ip=ip,
            expires_at=now_utc() + timedelta(minutes=ttl_min),
        )
        self.db.add(code)
        self.db.flush()
        return code


class SessionRepo:
    def __init__(self, db: DbSession):
        self.db = db

    def create(self, user_id: int, token_hash: str, user_agent: str, ip: str | None) -> Session:
        session = Session(user_id=user_id, token_hash=token_hash, user_agent=user_agent, ip=ip)
        self.db.add(session)
        self.db.flush()
        return session

    def by_token_hash(self, token_hash: str) -> Session | None:
        return self.db.scalar(
            select(Session).where(
                Session.token_hash == token_hash, Session.revoked_at.is_(None)
            )
        )

    def by_id_for_user(self, session_id: uuid.UUID, user_id: int) -> Session | None:
        return self.db.scalar(
            select(Session).where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
            )
        )

    def list_for_user(self, user_id: int) -> list[Session]:
        return list(
            self.db.scalars(
                select(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .order_by(Session.last_seen_at.desc())
            )
        )

    def revoke(self, session: Session) -> None:
        session.revoked_at = now_utc()

    def revoke_others(self, user_id: int, keep_id: uuid.UUID) -> int:
        result = self.db.execute(
            update(Session)
            .where(
                Session.user_id == user_id,
                Session.id != keep_id,
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=now_utc())
        )
        return result.rowcount

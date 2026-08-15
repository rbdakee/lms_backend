from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.adapters.db.models import User
from app.domain.profile import onboarding_done


class RequestCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    consent: bool


class RequestCodeOut(BaseModel):
    retry_after_sec: int


class VerifyCodeIn(BaseModel):
    phone: str = Field(max_length=20)
    code: str = Field(max_length=10)


class UserOut(BaseModel):
    id: int
    phone: str
    first_name: str
    last_name: str
    middle_name: str
    photo_url: str | None
    email: str
    school: str
    position: str
    region: str
    city: str
    subject: str
    experience: int | None
    lang: str
    is_admin: bool
    onboarding_done: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            phone=user.phone,
            first_name=user.first_name,
            last_name=user.last_name,
            middle_name=user.middle_name,
            photo_url=user.photo_url,
            email=user.email,
            school=user.school,
            position=user.position,
            region=user.region,
            city=user.city,
            subject=user.subject,
            experience=user.experience,
            lang=user.lang,
            is_admin=user.is_admin,
            onboarding_done=onboarding_done(user.first_name, user.last_name),
            created_at=user.created_at,
        )


class UserPatch(BaseModel):
    """Все поля необязательные: PATCH меняет только присланное."""

    model_config = {"extra": "forbid"}

    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    middle_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=320)
    school: str | None = Field(None, max_length=300)
    position: str | None = Field(None, max_length=200)
    region: str | None = Field(None, max_length=100)
    city: str | None = Field(None, max_length=100)
    subject: str | None = Field(None, max_length=100)
    experience: int | None = Field(None, ge=0, le=70)
    lang: Literal["ru", "kz"] | None = None


class SessionOut(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    user_agent: str
    is_current: bool


class SessionListOut(BaseModel):
    items: list[SessionOut]


class LogoutOthersOut(BaseModel):
    revoked_count: int


class CategoryOut(BaseModel):
    id: int
    title: str


class DictionariesOut(BaseModel):
    regions: list[str]
    subjects: list[str]
    categories: list[CategoryOut]

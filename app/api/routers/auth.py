from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.adapters.db.models import Session
from app.api import deps
from app.api.schemas import (
    LogoutOthersOut,
    RequestCodeIn,
    RequestCodeOut,
    UserOut,
    VerifyCodeIn,
)
from app.application.auth import AuthService
from app.config import get_settings

router = APIRouter(prefix="/auth")


@router.post("/request_code")
def request_code(
    body: RequestCodeIn,
    request: Request,
    svc: Annotated[AuthService, Depends(deps.get_auth_service)],
) -> RequestCodeOut:
    retry = svc.request_code(body.phone, body.consent, deps.get_client_ip(request))
    return RequestCodeOut(retry_after_sec=retry)


@router.post("/verify_code")
def verify_code(
    body: VerifyCodeIn,
    request: Request,
    response: Response,
    svc: Annotated[AuthService, Depends(deps.get_auth_service)],
) -> UserOut:
    user, token = svc.verify_code(
        body.phone,
        body.code,
        request.headers.get("user-agent", ""),
        deps.get_client_ip(request),
    )
    deps.set_session_cookie(request, response, token, get_settings())
    return UserOut.from_user(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    svc: Annotated[AuthService, Depends(deps.get_auth_service)],
) -> None:
    # Идемпотентно: выход без живой сессии — тоже успех
    token = request.cookies.get(deps.cookie_name(request))
    if token:
        svc.logout(token)
    deps.clear_session_cookie(request, response, get_settings())


@router.post("/logout_others")
def logout_others(
    session: Annotated[Session, Depends(deps.get_current_session)],
    svc: Annotated[AuthService, Depends(deps.get_auth_service)],
) -> LogoutOthersOut:
    return LogoutOthersOut(revoked_count=svc.logout_others(session))

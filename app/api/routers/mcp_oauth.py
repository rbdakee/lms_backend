"""OAuth 2.1 для MCP: бэкенд — сам авторизационный сервер (`MCP_BRIEF`, решение 3).

Метаданные (RFC 9728, RFC 8414), динамическая регистрация клиента (RFC 7591),
вход в авторизацию и обмен кода на токены. Экран согласия — в админке:
`/oauth/authorize` только проверяет параметры и переводит туда браузер.

Ошибки здесь не в общем формате сервиса, а в формате RFC 6749
(`error`, `error_description`): их читают клиенты, а не наш фронт.
"""

import json
from typing import Annotated
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.base import get_db
from app.api import deps
from app.application.mcp_access import SCOPE, McpAccessService, OAuthError
from app.application.ratelimit import SlidingWindowLimiter

router = APIRouter(tags=["mcp_oauth"])

# Регистрация открыта всем — так устроен RFC 7591, — и без лимита её можно
# использовать, чтобы забить таблицу клиентов
_register_limiter = SlidingWindowLimiter(20, 3600)

Svc = Annotated[McpAccessService, Depends(deps.get_mcp_access_service)]
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _error(exc: OAuthError, db: DbSession | None = None) -> JSONResponse:
    # Код мог быть сожжён до отказа (обмен с неверным verifier): запись должна
    # пережить ошибку, а `get_db` на ответе без исключения коммитит сам, но
    # здесь коммит явный — поведение не должно зависеть от чужого кода
    if db is not None:
        db.commit()
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=exc.status,
        headers=NO_STORE,
    )


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(svc: Svc) -> dict:
    """RFC 9728. Второй путь — вариант с суффиксом ресурса: клиенты строят
    адрес метаданных из адреса `/mcp` и ходят по нему первым."""
    return {
        "resource": svc.resource,
        "authorization_servers": [svc.issuer],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "LMS: действия администратора",
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server(svc: Svc) -> dict:
    """RFC 8414."""
    return {
        "issuer": svc.issuer,
        "authorization_endpoint": f"{svc.issuer}/oauth/authorize",
        "token_endpoint": f"{svc.issuer}/oauth/token",
        "registration_endpoint": f"{svc.issuer}/oauth/register",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Клиенты публичные: секрета нет, код защищает PKCE, и только S256
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    }


@router.post("/oauth/register", status_code=201)
async def register(request: Request, svc: Svc, db: Annotated[DbSession, Depends(get_db)]):
    ip = deps.get_client_ip(request) or "?"
    if _register_limiter.hit(ip):
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "Слишком много регистраций"},
            status_code=429,
        )
    try:
        meta = json.loads(await request.body() or b"{}")
        if not isinstance(meta, dict):
            raise ValueError
    except ValueError:
        return _error(OAuthError("invalid_client_metadata", "Тело — JSON-объект"))
    try:
        return JSONResponse(svc.register_client(meta), status_code=201, headers=NO_STORE)
    except OAuthError as exc:
        return _error(exc)


@router.get("/oauth/authorize")
def authorize(request: Request, svc: Svc):
    """Проверяет запрос и отправляет браузер на экран согласия в админке."""
    params = dict(request.query_params)
    try:
        client, error = svc.check_authorize(params)
    except OAuthError as exc:
        # Клиент или redirect_uri не опознаны: возвращать человека некуда
        return _error(exc)
    if error is not None:
        back = {"error": error}
        if params.get("state"):
            back["state"] = params["state"]
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(params["redirect_uri"] + sep + urlencode(back), status_code=302)
    return RedirectResponse(svc.consent_url(params), status_code=302)


@router.post("/oauth/token")
async def token(request: Request, svc: Svc, db: Annotated[DbSession, Depends(get_db)]):
    # Тело разбирается руками: форма RFC 6749 — это urlencoded, а читать её
    # через `Form` значило бы тянуть отдельный пакет ради четырёх полей
    form = dict(parse_qsl((await request.body()).decode("utf-8", "replace")))
    grant = form.get("grant_type")
    try:
        if grant == "authorization_code":
            result = svc.exchange_code(form)
        elif grant == "refresh_token":
            result = svc.refresh(form)
        else:
            raise OAuthError(
                "unsupported_grant_type", "Поддержаны authorization_code и refresh_token"
            )
    except OAuthError as exc:
        return _error(exc, db)
    return JSONResponse(result, headers=NO_STORE)

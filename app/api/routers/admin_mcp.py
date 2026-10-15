"""Админская сторона MCP: экран согласия и экран «Подключения ИИ».

Всё за обычной админской авторизацией по куке, как прочие `/admin/*`:
своего входа у MCP нет (`MCP_BRIEF`, решение 3).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.adapters.db.models import McpConnection, User
from app.api import deps
from app.api.schemas_mcp import (
    McpAuthorizeClientOut,
    McpAuthorizeIn,
    McpAuthorizeInfoOut,
    McpAuthorizeOut,
    McpConnectionOut,
    McpConnectionPatchIn,
    McpConnectionsOut,
    McpTokenIn,
    McpTokenOut,
)
from app.application.mcp_access import McpAccessService, OAuthError
from app.domain.errors import ValidationAppError

router = APIRouter(prefix="/admin/mcp")

AccessSvc = Annotated[McpAccessService, Depends(deps.get_mcp_access_service)]
Admin = Annotated[User, Depends(deps.get_current_admin)]

_AUTHORIZE_KEYS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
)


def _connection_out(conn: McpConnection) -> McpConnectionOut:
    return McpConnectionOut(
        id=conn.id,
        name=conn.name,
        kind=conn.kind,
        pd_allowed=conn.pd_allowed,
        created_at=conn.created_at,
    )


@router.get("/authorize_info")
def authorize_info(request: Request, admin: Admin, svc: AccessSvc) -> McpAuthorizeInfoOut:
    """Что показать на экране согласия: кто просит доступ и годится ли запрос.

    Параметры — те же, что пришли в `/oauth/authorize`, прямо из адреса экрана.
    Негодный запрос — 200 с `valid=false`: экран рисует «Запрос недействителен»,
    а не общую ошибку.
    """
    params = {
        key: request.query_params[key] for key in _AUTHORIZE_KEYS if key in request.query_params
    }
    try:
        client, error = svc.check_authorize(params)
    except OAuthError as exc:
        return McpAuthorizeInfoOut(valid=False, error_code=exc.error, error_message=exc.description)
    return McpAuthorizeInfoOut(
        valid=error is None,
        error_code=error,
        error_message=None if error is None else "Клиент прислал неполный или неверный запрос",
        client=McpAuthorizeClientOut(client_id=client.client_id, name=client.name),
        redirect_uri=params["redirect_uri"],
    )


@router.post("/authorize")
def authorize(body: McpAuthorizeIn, admin: Admin, svc: AccessSvc) -> McpAuthorizeOut:
    params = {
        key: value
        for key, value in body.model_dump(include=set(_AUTHORIZE_KEYS)).items()
        if value is not None
    }
    try:
        redirect_to = svc.decide(admin, params, body.decision == "allow", body.pd_allowed)
    except OAuthError as exc:
        # Клиент или redirect_uri не опознаны: вернуть человека по такому адресу
        # нельзя, ошибка показывается на месте
        raise ValidationAppError(exc.description) from exc
    return McpAuthorizeOut(redirect_to=redirect_to)


@router.post("/tokens")
def create_token(body: McpTokenIn, admin: Admin, svc: AccessSvc) -> McpTokenOut:
    conn, token = svc.create_personal_token(admin, body.name, body.pd_allowed)
    return McpTokenOut(**_connection_out(conn).model_dump(), token=token)


@router.get("/connections")
def connections(admin: Admin, svc: AccessSvc) -> McpConnectionsOut:
    return McpConnectionsOut(items=[_connection_out(c) for c in svc.connections(admin)])


@router.patch("/connections/{connection_id}")
def patch_connection(
    connection_id: int, body: McpConnectionPatchIn, admin: Admin, svc: AccessSvc
) -> McpConnectionOut:
    return _connection_out(svc.set_pd(admin, connection_id, body.pd_allowed))


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_connection(connection_id: int, admin: Admin, svc: AccessSvc) -> None:
    svc.revoke(admin, connection_id)

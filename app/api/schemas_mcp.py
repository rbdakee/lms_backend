"""Схемы ручек MCP: экран согласия и экран «Подключения ИИ» в админке."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class McpAuthorizeParams(BaseModel):
    """Те же параметры, с которыми клиент пришёл на `/oauth/authorize`."""

    client_id: str
    redirect_uri: str
    response_type: str = "code"
    code_challenge: str = ""
    code_challenge_method: str = ""
    state: str | None = None
    scope: str | None = None
    resource: str | None = None


class McpAuthorizeClientOut(BaseModel):
    client_id: str
    name: str


class McpAuthorizeInfoOut(BaseModel):
    valid: bool
    # Код и текст для экрана, когда valid=false: «Запрос недействителен»
    error_code: str | None = None
    error_message: str | None = None
    client: McpAuthorizeClientOut | None = None
    redirect_uri: str | None = None
    scope: str = "mcp"


class McpAuthorizeIn(McpAuthorizeParams):
    decision: Literal["allow", "deny"]
    pd_allowed: bool = False


class McpAuthorizeOut(BaseModel):
    redirect_to: str


class McpTokenIn(BaseModel):
    name: str = Field(max_length=200)
    pd_allowed: bool = False


class McpConnectionOut(BaseModel):
    id: int
    name: str
    kind: Literal["oauth", "token"]
    pd_allowed: bool
    created_at: datetime


class McpTokenOut(McpConnectionOut):
    # Показывается один раз: в базе остаётся только хеш
    token: str


class McpConnectionsOut(BaseModel):
    items: list[McpConnectionOut]


class McpConnectionPatchIn(BaseModel):
    pd_allowed: bool

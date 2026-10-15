"""Streamable HTTP-эндпоинт `/mcp`: `tools/list` из реестра, `tools/call` в реестр.

Без состояния (`stateless`): сессий MCP нет, каждый запрос самодостаточен, и
это же снимает вопрос «чья сессия» — подключение узнаётся по токену на каждом
запросе. Ответы обычным JSON, без SSE: инструменты быстрые, потоков у нас нет.

Токен проверяется дважды и оба раза в базе: на входе (чтобы отдать 401 с
подсказкой клиенту, где искать авторизацию) и в самом `tools/call` (чтобы отзыв
и снятие админа действовали и на пакетный запрос, и на любой следующий вызов).
"""

import contextlib
import json
import logging
from contextvars import ContextVar

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.adapters.db.base import get_engine
from app.adapters.db.mcp_repos import McpRepo
from app.adapters.db.repos import now_utc
from app.application.mcp_access import McpAccessService
from app.application.ratelimit import SlidingWindowLimiter
from app.config import get_settings
from app.domain.errors import AppError

log = logging.getLogger("app.mcp")

# Токен текущего запроса. Обработчик MCP живёт в задаче, которую создаёт SDK;
# контекст при этом копируется из задачи запроса, где токен уже лежит
_token: ContextVar[str | None] = ContextVar("mcp_token", default=None)

# Счётчик в памяти процесса, как у остальных лимитов (`ratelimit.py`). Ключ —
# подключение, а не админ: одно зациклившееся подключение не должно
# отключать остальные
_limiter = SlidingWindowLimiter(get_settings().mcp_calls_per_min, 60)

INSTRUCTIONS = (
    "Действия администратора учебной платформы LMS: курсы, учителя, заявки, отчёты."
    " Инструменты с пометкой destructive выполняются сразу — спрашивай человека до вызова."
    " Персональные данные скрыты, пока подключение не допущено к ним в админке."
)


def _annotations(danger: str) -> types.ToolAnnotations:
    """Уровень реестра → подсказки MCP; по ним клиент сам решает, спрашивать ли человека."""
    return types.ToolAnnotations(
        read_only_hint=danger == "read",
        destructive_hint=danger == "confirm",
        # Мир закрыт: инструмент ходит только в нашу базу
        open_world_hint=False,
    )


def build_tools() -> list[types.Tool]:
    from app.assistant import registry

    return [
        types.Tool(
            name=spec.name,
            description=spec.full_description(),
            input_schema=spec.input_schema(),
            annotations=_annotations(spec.danger),
        )
        for _, spec in sorted(registry.all_tools().items())
    ]


def _text(payload: dict, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(payload, ensure_ascii=False, default=str))],
        is_error=is_error,
    )


def run_tool(token: str, name: str, args: dict) -> types.CallToolResult:
    """Один вызов: проверка допуска, лимит, реестр, журнал. Синхронный — база синхронная."""
    from app.assistant import registry
    from app.assistant.registry import ToolCtx, ToolError, mask_args

    cfg = get_settings()
    with DbSession(get_engine()) as db:
        repo = McpRepo(db)
        found = McpAccessService(repo, cfg).authenticate(token)
        if found is None:
            return _text(
                {"error": {"code": "unauthorized", "message": "Подключение отозвано"}},
                is_error=True,
            )
        conn, admin = found

        spec = registry.all_tools().get(name)
        if spec is None:
            repo.log_call(conn.id, name[:100], {}, False, "unknown_tool")
            db.commit()
            return _text(
                {"error": {"code": "unknown_tool", "message": f"Инструмента {name[:100]} нет"}},
                is_error=True,
            )
        # В журнал — только замаскированные аргументы; значения персональных
        # полей туда не попадают ни при успехе, ни при ошибке
        masked = mask_args(spec, args)

        if _limiter.hit(str(conn.id)):
            repo.log_call(conn.id, name, masked, False, "rate_limited")
            db.commit()
            return _text(
                {
                    "error": {
                        "code": "rate_limited",
                        "message": "Слишком много вызовов в минуту — подожди и повтори",
                    }
                },
                is_error=True,
            )

        ctx = ToolCtx(db=db, admin=admin, now=now_utc(), pd_allowed=conn.pd_allowed)
        try:
            result = registry.call(name, args, ctx)
            db.commit()
        except ToolError as exc:
            db.rollback()
            repo.log_call(conn.id, name, masked, False, exc.code)
            db.commit()
            return _text(exc.as_result(), is_error=True)
        except AppError as exc:
            # Отказ сервиса по существу (нет такого курса, нельзя удалить):
            # его текст писали для экрана админки и персданных в нём нет
            db.rollback()
            repo.log_call(conn.id, name, masked, False, exc.code)
            db.commit()
            return _text({"error": {"code": exc.code, "message": exc.message}}, is_error=True)
        except Exception as exc:
            db.rollback()
            # Только тип: текст исключения может нести значения из запроса
            log.error("MCP: инструмент %s упал: %s", name, type(exc).__name__)
            repo.log_call(conn.id, name, masked, False, "internal_error")
            db.commit()
            return _text(
                {"error": {"code": "internal_error", "message": "Внутренняя ошибка инструмента"}},
                is_error=True,
            )
        repo.log_call(conn.id, name, masked, True, None)
        db.commit()
        return _text(result)


async def _list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=build_tools())


async def _call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    token = _token.get()
    if token is None:
        return _text({"error": {"code": "unauthorized", "message": "Нет токена"}}, is_error=True)
    return await run_in_threadpool(run_tool, token, params.name, params.arguments or {})


def _bearer(scope: Scope) -> str | None:
    for key, value in scope["headers"]:
        if key == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    return None


def _authenticate(token: str) -> bool:
    with DbSession(get_engine()) as db:
        return McpAccessService(McpRepo(db), get_settings()).authenticate(token) is not None


class McpEndpoint:
    """ASGI-приложение для маршрута `/mcp`. Менеджер SDK заводится в `lifespan`,
    а не при создании: он запускается один раз, а тесты поднимают приложение
    несколько раз подряд."""

    def __init__(self) -> None:
        self.manager: StreamableHTTPSessionManager | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self):
        server = Server(
            "lms-admin",
            instructions=INSTRUCTIONS,
            on_list_tools=_list_tools,
            on_call_tool=_call_tool,
        )
        manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True)
        async with manager.run():
            self.manager = manager
            try:
                yield
            finally:
                self.manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.manager is None:
            raise RuntimeError("MCP-эндпоинт не запущен")
        token = _bearer(scope)
        ok = token is not None and await run_in_threadpool(_authenticate, token)
        if not ok:
            await self._unauthorized(token is not None)(scope, receive, send)
            return
        reset = _token.set(token)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _token.reset(reset)

    @staticmethod
    def _unauthorized(had_token: bool) -> JSONResponse:
        """401 с подсказкой, где искать авторизацию (RFC 9728): так клиент
        узнаёт, что делать, и сам идёт на метаданные."""
        issuer = get_settings().public_base_url.rstrip("/")
        challenge = f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource"'
        if had_token:
            challenge += ', error="invalid_token"'
        return JSONResponse(
            {"error": "unauthorized", "error_description": "Нужен токен подключения"},
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )

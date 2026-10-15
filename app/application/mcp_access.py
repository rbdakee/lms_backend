"""Доступ к MCP: OAuth 2.1 (бэкенд — сам авторизационный сервер) и личные токены.

Решения — `MCP_BRIEF`, раздел «Что решено», пункт 3. Оба вида входа — строки
одной таблицы подключений. Токены случайные и непрозрачные, в базе лежит
только хеш. Права админа проверяются на каждом вызове (`authenticate`),
а не при выдаче: снятого админа отрезает сразу.

Домен здесь про протокол, а не про FastAPI: ошибки — `OAuthError` с кодами
RFC 6749, роутеры сами решают, как их показать.
"""

import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from app.adapters.db.mcp_repos import McpRepo
from app.adapters.db.models import McpConnection, User
from app.adapters.db.repos import now_utc
from app.application.auth import hash_token
from app.config import Settings
from app.domain.errors import NotFoundError, ValidationAppError

# Код авторизации живёт минуты: он нужен ровно на один переход «браузер → клиент → /oauth/token»
CODE_TTL = timedelta(minutes=5)
SCOPE = "mcp"
# Префиксы нужны глазу и сканерам секретов: по ним токен узнаётся в логе или в репозитории
ACCESS_PREFIX = "mcp_at_"
REFRESH_PREFIX = "mcp_rt_"
PERSONAL_PREFIX = "mcp_pat_"

NAME_MAX = 100
REDIRECTS_MAX = 10
# RFC 7636: verifier — от 43 до 128 знаков из безопасного алфавита
_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
_LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")


class OAuthError(Exception):
    """Ошибка по RFC 6749: `error` — машинный код, `description` — для разработчика клиента."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _redirect_ok(uri: str) -> bool:
    """Куда клиент просит вернуть код. `https` — любой; `http` — только петля
    (локальные клиенты вроде Codex и Claude Code слушают порт на своей машине);
    собственные схемы (`cursor://`) — приложениям на компьютере. Опасные схемы
    и адреса с фрагментом отбрасываются."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.scheme or len(uri) > 2000:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return parts.hostname in _LOOPBACK or f"[{parts.hostname}]" in _LOOPBACK
    return parts.scheme not in ("javascript", "data", "file", "vbscript", "about", "blob")


class McpAccessService:
    def __init__(self, repo: McpRepo, cfg: Settings):
        self.repo = repo
        self.cfg = cfg

    # -- адреса --------------------------------------------------------

    @property
    def issuer(self) -> str:
        return self.cfg.public_base_url.rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    def consent_url(self, params: dict[str, str]) -> str:
        """Экран согласия живёт в админке: админ уже вошёл по своему коду,
        своего входа у MCP нет."""
        base = self.cfg.admin_base_url.rstrip("/")
        return f"{base}/mcp/authorize?{urlencode(params)}"

    # -- регистрация клиента (RFC 7591) ----------------------------------

    def register_client(self, meta: dict) -> dict:
        uris = meta.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not uris
            or len(uris) > REDIRECTS_MAX
            or not all(isinstance(u, str) and _redirect_ok(u) for u in uris)
        ):
            raise OAuthError(
                "invalid_redirect_uri",
                "redirect_uris: нужен непустой список адресов — https, http на localhost"
                " или схема приложения",
            )
        name = meta.get("client_name")
        name = name.strip() if isinstance(name, str) else ""
        # Имя читает человек на экране согласия: управляющие знаки и простыня ему ни к чему
        name = "".join(ch for ch in name if ch.isprintable())[:NAME_MAX] or "Клиент без имени"
        client_id = "mcp_client_" + secrets.token_urlsafe(18)
        self.repo.add_client(client_id, name, list(dict.fromkeys(uris)))
        return {
            "client_id": client_id,
            "client_id_issued_at": int(now_utc().timestamp()),
            "client_name": name,
            "redirect_uris": list(dict.fromkeys(uris)),
            # Клиент публичный: защита кода — PKCE, секрета нет
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
        }

    # -- авторизация -----------------------------------------------------

    def check_authorize(self, params: dict[str, str]) -> tuple[object, str | None]:
        """Проверка запроса авторизации.

        Возвращает (клиент, ошибка-код-или-None). Если сам клиент или
        `redirect_uri` не опознаны — `OAuthError`: на такой адрес возвращать
        человека нельзя (RFC 6749, 4.1.2.1), ошибку показывают на месте.
        """
        client = self.repo.client(params.get("client_id") or "")
        if client is None:
            raise OAuthError("invalid_client", "Клиент не зарегистрирован")
        redirect_uri = params.get("redirect_uri") or ""
        if redirect_uri not in client.redirect_uris:
            raise OAuthError("invalid_request", "redirect_uri не совпадает с зарегистрированным")
        if params.get("response_type") != "code":
            return client, "unsupported_response_type"
        challenge = params.get("code_challenge") or ""
        if params.get("code_challenge_method") != "S256" or not _CHALLENGE.match(challenge):
            # Без PKCE S256 код не выдаётся вовсе (OAuth 2.1)
            return client, "invalid_request"
        resource = params.get("resource")
        if resource and not resource.rstrip("/").startswith(self.issuer):
            return client, "invalid_target"
        return client, None

    def decide(self, admin: User, params: dict[str, str], allow: bool, pd_allowed: bool) -> str:
        """Решение админа на экране согласия → куда вернуть браузер."""
        client, error = self.check_authorize(params)
        redirect_uri = params["redirect_uri"]
        out: dict[str, str] = {}
        if error is not None:
            out["error"] = error
        elif not allow:
            out["error"] = "access_denied"
        else:
            code = new_token("")
            self.repo.add_code(
                code_hash=hash_token(code),
                client_id=client.client_id,
                admin_id=admin.id,
                redirect_uri=redirect_uri,
                code_challenge=params["code_challenge"],
                pd_allowed=pd_allowed,
                expires_at=now_utc() + CODE_TTL,
            )
            out["code"] = code
        if params.get("state"):
            out["state"] = params["state"]
        sep = "&" if "?" in redirect_uri else "?"
        return redirect_uri + sep + urlencode(out)

    # -- токены (RFC 6749, 4.1.3 и 6) ------------------------------------

    def _issue(self, conn: McpConnection, now: datetime) -> dict:
        access = new_token(ACCESS_PREFIX)
        refresh = new_token(REFRESH_PREFIX)
        ttl = timedelta(minutes=self.cfg.mcp_access_ttl_min)
        conn.access_hash = hash_token(access)
        conn.access_expires_at = now + ttl
        conn.refresh_hash = hash_token(refresh)
        conn.refresh_expires_at = now + timedelta(days=self.cfg.mcp_refresh_ttl_days)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(ttl.total_seconds()),
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    def _live_admin(self, user_id: int) -> User | None:
        user = self.repo.user(user_id)
        if user is None or not user.is_admin or user.is_blocked:
            return None
        return user

    def exchange_code(self, form: dict[str, str]) -> dict:
        now = now_utc()
        code = form.get("code") or ""
        # Код сгорает при первой попытке, удачной или нет: подбирать verifier
        # на живом коде нельзя
        row = self.repo.claim_code(hash_token(code), now) if code else None
        if row is None:
            raise OAuthError("invalid_grant", "Код неизвестен или уже использован")
        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "Срок кода истёк")
        if form.get("client_id") != row.client_id:
            raise OAuthError("invalid_grant", "Код выдан другому клиенту")
        if form.get("redirect_uri") != row.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri не совпадает с запросом авторизации")
        verifier = form.get("code_verifier") or ""
        if not _VERIFIER.match(verifier) or not secrets.compare_digest(
            pkce_challenge(verifier), row.code_challenge
        ):
            raise OAuthError("invalid_grant", "code_verifier не подходит к code_challenge")
        resource = form.get("resource")
        if resource and not resource.rstrip("/").startswith(self.issuer):
            raise OAuthError("invalid_target", "Неизвестный ресурс")
        if self._live_admin(row.admin_id) is None:
            raise OAuthError("invalid_grant", "Права администратора сняты")
        client = self.repo.client(row.client_id)
        conn = self.repo.add_connection(
            admin_id=row.admin_id,
            kind="oauth",
            name=client.name if client else "Клиент",
            client_id=row.client_id,
            pd_allowed=row.pd_allowed,
            access_hash=hash_token(secrets.token_urlsafe(8)),  # затирается в _issue
        )
        return self._issue(conn, now)

    def refresh(self, form: dict[str, str]) -> dict:
        now = now_utc()
        token = form.get("refresh_token") or ""
        conn = self.repo.by_refresh_hash(hash_token(token)) if token else None
        if (
            conn is None
            or conn.revoked_at is not None
            or conn.refresh_expires_at is None
            or conn.refresh_expires_at <= now
        ):
            raise OAuthError("invalid_grant", "Токен обновления недействителен")
        if form.get("client_id") and form["client_id"] != conn.client_id:
            raise OAuthError("invalid_grant", "Токен выдан другому клиенту")
        if self._live_admin(conn.admin_id) is None:
            raise OAuthError("invalid_grant", "Права администратора сняты")
        # Ротация: старый токен обновления перестаёт работать в тот же момент
        return self._issue(conn, now)

    # -- личные токены и экран «Подключения ИИ» --------------------------

    def create_personal_token(
        self, admin: User, name: str, pd_allowed: bool
    ) -> tuple[McpConnection, str]:
        name = name.strip()
        if not name or len(name) > NAME_MAX:
            raise ValidationAppError(f"Имя токена — от 1 до {NAME_MAX} знаков")
        token = new_token(PERSONAL_PREFIX)
        conn = self.repo.add_connection(
            admin_id=admin.id,
            kind="token",
            name=name,
            pd_allowed=pd_allowed,
            access_hash=hash_token(token),
        )
        return conn, token

    def connections(self, admin: User) -> list[McpConnection]:
        return self.repo.active_of(admin.id)

    def set_pd(self, admin: User, connection_id: int, pd_allowed: bool) -> McpConnection:
        conn = self.repo.own_active(admin.id, connection_id)
        if conn is None:
            raise NotFoundError("Подключения нет")
        conn.pd_allowed = pd_allowed
        return conn

    def revoke(self, admin: User, connection_id: int) -> None:
        conn = self.repo.own_active(admin.id, connection_id)
        if conn is None:
            raise NotFoundError("Подключения нет")
        conn.revoked_at = now_utc()

    # -- проверка на каждом вызове /mcp ----------------------------------

    def authenticate(self, token: str) -> tuple[McpConnection, User] | None:
        """Токен → подключение и админ, или None. Неотозванность, срок и
        `is_admin` смотрятся здесь, на каждом вызове (решение 3)."""
        conn = self.repo.by_access_hash(hash_token(token))
        if conn is None or conn.revoked_at is not None:
            return None
        if conn.access_expires_at is not None and conn.access_expires_at <= now_utc():
            return None
        admin = self._live_admin(conn.admin_id)
        if admin is None:
            return None
        return conn, admin

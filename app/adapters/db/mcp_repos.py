"""Хранилище MCP: клиенты, коды, подключения, журнал вызовов."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.models import McpAuthCode, McpCallLog, McpClient, McpConnection, User


class McpRepo:
    def __init__(self, db: DbSession):
        self.db = db

    # -- клиенты ---------------------------------------------------------

    def add_client(self, client_id: str, name: str, redirect_uris: list[str]) -> McpClient:
        client = McpClient(client_id=client_id, name=name, redirect_uris=redirect_uris)
        self.db.add(client)
        self.db.flush()
        return client

    def client(self, client_id: str) -> McpClient | None:
        return self.db.query(McpClient).filter(McpClient.client_id == client_id).one_or_none()

    # -- коды авторизации ------------------------------------------------

    def add_code(self, **fields) -> McpAuthCode:
        code = McpAuthCode(**fields)
        self.db.add(code)
        self.db.flush()
        return code

    def claim_code(self, code_hash: str, now: datetime) -> McpAuthCode | None:
        """Код берётся атомарно: из двух одновременных обменов одного кода
        строку получит только один — второй увидит `used_at` и уйдёт ни с чем."""
        claimed = self.db.execute(
            update(McpAuthCode)
            .where(McpAuthCode.code_hash == code_hash, McpAuthCode.used_at.is_(None))
            .values(used_at=now)
            .returning(McpAuthCode.id)
        ).scalar_one_or_none()
        if claimed is None:
            return None
        return self.db.get(McpAuthCode, claimed)

    # -- подключения -----------------------------------------------------

    def add_connection(self, **fields) -> McpConnection:
        conn = McpConnection(**fields)
        self.db.add(conn)
        self.db.flush()
        return conn

    def by_access_hash(self, access_hash: str) -> McpConnection | None:
        return (
            self.db.query(McpConnection)
            .filter(McpConnection.access_hash == access_hash)
            .one_or_none()
        )

    def by_refresh_hash(self, refresh_hash: str) -> McpConnection | None:
        # С блокировкой строки: два обновления одним токеном идут по очереди,
        # и второе уже не найдёт старый хеш
        return (
            self.db.query(McpConnection)
            .filter(McpConnection.refresh_hash == refresh_hash)
            .with_for_update()
            .one_or_none()
        )

    def active_of(self, admin_id: int) -> list[McpConnection]:
        return (
            self.db.query(McpConnection)
            .filter(McpConnection.admin_id == admin_id, McpConnection.revoked_at.is_(None))
            .order_by(McpConnection.created_at.desc(), McpConnection.id.desc())
            .all()
        )

    def own_active(self, admin_id: int, connection_id: int) -> McpConnection | None:
        return (
            self.db.query(McpConnection)
            .filter(
                McpConnection.id == connection_id,
                McpConnection.admin_id == admin_id,
                McpConnection.revoked_at.is_(None),
            )
            .one_or_none()
        )

    def user(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    # -- журнал ----------------------------------------------------------

    def log_call(
        self, connection_id: int, tool: str, args: dict, ok: bool, error_code: str | None
    ) -> None:
        self.db.add(
            McpCallLog(
                connection_id=connection_id, tool=tool, args=args, ok=ok, error_code=error_code
            )
        )
        self.db.flush()

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_db() -> Iterator[Session]:
    """Сессия БД на один запрос: успех — commit, исключение — rollback.

    Там, где запись должна пережить ошибку (счётчик попыток ввода кода),
    сценарий коммитит сам до того, как поднять AppError.
    """
    db = Session(get_engine())
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

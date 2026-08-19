"""Хранилище на локальном диске.

Отдельной заглушки у порта `storage` нет и не нужно: локальный каталог
и есть версия для разработки — файл реально сохраняется и реально
раздаётся, в отличие от кода в логе у SMS. В бою на это же место встанет
адаптер объектного хранилища, и меняется только строчка конфигурации.
"""

from collections.abc import Iterable, Iterator
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def save(self, key: str, chunks: Iterable[bytes]) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with path.open("wb") as out:
                for chunk in chunks:
                    out.write(chunk)
                    size += len(chunk)
        except Exception:
            # Превышен лимит или оборвалась загрузка: недописанный объект
            # не должен остаться лежать под уже выданным ключом
            path.unlink(missing_ok=True)
            raise
        return size

    def size(self, key: str) -> int | None:
        # Сюда ключ приходит снаружи — из тела PATCH: «уводит за пределы
        # каталога» для него значит «такого объекта нет», и сценарий отдаёт
        # контрактное 404, а не 500. У save и read ключ наш собственный,
        # и там выход за каталог остаётся исключением.
        try:
            path = self._path(key)
        except ValueError:
            return None
        return path.stat().st_size if path.is_file() else None

    def read(self, key: str) -> Iterator[bytes]:
        with self._path(key).open("rb") as src:
            while chunk := src.read(CHUNK_SIZE):
                yield chunk

    def _path(self, key: str) -> Path:
        """Ключ в базе ставит админ, а на диске это путь: `../../etc/passwd`
        не должен уводить за пределы каталога хранилища."""
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Ключ уводит за пределы хранилища")
        return path

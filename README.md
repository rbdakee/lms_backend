# Бэкенд LMS

FastAPI + PostgreSQL. Правила работы — `CLAUDE.md`, форма ответов API —
`CONTRACT.md`, модель данных и решения — `../BACKEND_NOTES.md`.

## Запуск

Одной командой (Postgres + API с миграциями):

```sh
docker compose up --build
```

API на `http://localhost:8000`, OpenAPI-схема — `http://localhost:8000/docs`.

Для разработки удобнее база в докере, сервис — локально:

```sh
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

SMS-провайдер — заглушка: код входа пишется в лог сервиса (`SMS на ***4567: код 1234`).

Появилась новая зависимость — контейнер поднимать с `--renew-anon-volumes`:
`.venv` внутри него лежит в анонимном томе, и пересборка образа его не трогает,
так что сервис падает на `ModuleNotFoundError` при живом образе.

```sh
docker compose up -d --build --renew-anon-volumes api
```

## Проверки

```sh
uv run ruff check .
uv run pytest
```

Тесты ходят в отдельную базу `lms_test` (создаётся автоматически при первом
старте контейнера базы) и чистят её между тестами.

## Структура

```
app/
  domain/        правила и ошибки: телефон, онбординг, справочники
  application/   сценарии и порты — что домену нужно от внешнего мира
  adapters/      реализации портов: Postgres (модели, репозитории), SMS
  api/           FastAPI: роутеры, схемы, единый формат ошибок
migrations/      alembic; вниз не откатываемся — чиним новой миграцией
tests/           pytest, против настоящего Postgres
```

Миграции: `uv run alembic revision --autogenerate -m "..."` после правки
`app/adapters/db/models.py`, затем `uv run alembic upgrade head`.

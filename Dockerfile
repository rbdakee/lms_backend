FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code

# Окружение вне /code: локальный compose монтирует исходники поверх /code,
# и лежи .venv там же, её пришлось бы спасать томом, который переживает
# пересборку и прячет от сервиса новые зависимости
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Зависимости — отдельным слоем, чтобы правка кода их не пересобирала
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Воркер ровно один, и это не умолчание, а решение: счётчики playback,
# публичной проверки сертификата и вопросов лежат в памяти процесса
# (app/application/ratelimit.py). Второй процесс удваивает каждый потолок,
# четвёртый — учетверяет, и происходит это молча, без единой ошибки.
# Менять число — только вместе с переездом этих счётчиков в базу.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

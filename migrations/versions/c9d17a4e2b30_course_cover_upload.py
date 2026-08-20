# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""course cover upload

Обложка курса становится загруженным файлом, а не адресом картинки,
вписанным руками: у курса появляется пара «ключ объекта в хранилище + имя
файла», как у картинок настроек и у шаблона задания.

Старая колонка `cover` остаётся и не чистится. У курсов, заведённых раньше,
в ней лежат внешние адреса, и посреди ручного QA они обязаны продолжать
работать: пока загруженной картинки нет, наружу уходит то, что лежало
в `cover`. Задать этот адрес через API больше нельзя — колонка стала
переходной, только на чтение.

Revision ID: c9d17a4e2b30
Revises: b8e5127d4a03
Create Date: 2026-08-20 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'c9d17a4e2b30'
down_revision = 'b8e5127d4a03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Обе колонки пустые: перенести внешний адрес в хранилище нечем — файла
    # у сервера нет, есть только ссылка на чужой сайт
    op.add_column('course', sa.Column('cover_key', sa.Text(), nullable=True))
    op.add_column('course', sa.Column('cover_name', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('course', 'cover_name')
    op.drop_column('course', 'cover_key')

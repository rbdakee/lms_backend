# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""task template_file_name

Имя файла-шаблона, которое видит учитель. До сессии 7а его выводили из ключа
хранилища (`PurePosixPath(task.template_file).name`), и это работало ровно
до появления редактора: ключи выдаёт POST /files, а он их делает случайными
(`uploads/2026/08/19/9f3c1a7e4b2d8c05.docx`) — имя от человека в ключ
не попадает нарочно, иначе им уводят путь из каталога.

Значит, без своей колонки учитель на экране задания увидел бы
«9f3c1a7e4b2d8c05.docx» вместо «Шаблон дескрипторов.docx». У материалов
урока имя лежит колонкой с первой миграции (`lesson_file.name`) — здесь
делаем то же самое.

Старым строкам проставляется имя из ключа: то самое, что показывалось
до сих пор, — экран не должен измениться задним числом.

Revision ID: f2a640c8b1d7
Revises: d5b83c1e7f40
Create Date: 2026-08-19 16:40:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'f2a640c8b1d7'
down_revision = 'd5b83c1e7f40'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('template_file_name', sa.Text(), nullable=True))
    op.execute(
        "UPDATE task SET template_file_name = regexp_replace(template_file, '^.*/', '')"
        " WHERE template_file IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column('task', 'template_file_name')

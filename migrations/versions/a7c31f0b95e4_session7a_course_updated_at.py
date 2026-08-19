# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""session7a course updated_at

Столбец «Изменён» в списке курсов админки. Считать его по created_at
дочерних строк нельзя: правка названия урока меняет курс, а строк
не добавляет, и колонка бы врала ровно там, где на неё смотрят.

Существующим курсам проставляется created_at, а не now(): курс, который
никто не трогал с июля, не должен выглядеть свежеправленым.

Revision ID: a7c31f0b95e4
Revises: e171728b2ae2
Create Date: 2026-08-19 12:10:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'a7c31f0b95e4'
down_revision = 'e171728b2ae2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'course',
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.execute('UPDATE course SET updated_at = created_at')


def downgrade() -> None:
    op.drop_column('course', 'updated_at')

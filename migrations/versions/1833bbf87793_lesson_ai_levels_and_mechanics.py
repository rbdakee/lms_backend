# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""lesson_ai levels and mechanics

Шесть остальных видов материалов «Жоспара» (TEACHER_AI_BRIEF, разделы 4.2 и 4.3;
сессия L5).

1. `lesson_kit.engagement_level` (`calm | active | full_game`) и
   `lesson_kit.difficulty_level` (`basic | standard | advanced`) — два уровня заказа
   вместо флага «геймификация». Уже заведённые комплекты получают середину —
   `active` и `standard`: это и есть значение по умолчанию из брифа, и именно так
   генерировались их планы (уровней тогда не было).
2. `lesson_material.mechanics` — слаги механик из каталога игр, которые сервер
   отдал модели как строительные блоки. Массив строк, не внешний ключ: каталог
   живёт файлами, таблицы механик нет (`L5_games_db_design.md`, раздел 5).
   У старых материалов — пустой массив.

Виды материалов в `CHECK` уже все семь — с L3, расширять незачем.

Revision ID: 1833bbf87793
Revises: 52101890dfd7
Create Date: 2026-09-22 23:41:10.980982

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '1833bbf87793'
down_revision = '52101890dfd7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('lesson_kit', sa.Column('engagement_level', sa.Text(), server_default='active', nullable=False))
    op.add_column('lesson_kit', sa.Column('difficulty_level', sa.Text(), server_default='standard', nullable=False))
    op.create_check_constraint(op.f('ck_lesson_kit_difficulty_level'), 'lesson_kit', "difficulty_level IN ('basic', 'standard', 'advanced')")
    op.create_check_constraint(op.f('ck_lesson_kit_engagement_level'), 'lesson_kit', "engagement_level IN ('calm', 'active', 'full_game')")
    op.add_column('lesson_material', sa.Column('mechanics', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('lesson_material', 'mechanics')
    op.drop_constraint(op.f('ck_lesson_kit_engagement_level'), 'lesson_kit', type_='check')
    op.drop_constraint(op.f('ck_lesson_kit_difficulty_level'), 'lesson_kit', type_='check')
    op.drop_column('lesson_kit', 'difficulty_level')
    op.drop_column('lesson_kit', 'engagement_level')

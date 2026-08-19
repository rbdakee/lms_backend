# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""session7b people and platform

Четыре несвязанные вещи одной миграцией — они приезжают одной сессией
и порознь не нужны.

1. Категории курсов переезжают из констант в таблицу: бриф 5.25 обещает их
   правку в настройках, а `domain/dictionaries.py` держал их списком в коде.
   Шесть нынешних переносятся с теми же id — на них уже ссылаются курсы,
   и смена id осиротила бы каталог.

2. «Разрешить пересдачу» снимает зачёт с попытки, а не удаляет её. Причина
   и админ остаются у самой попытки: у действий админа хранится actor_id,
   а на вкладке «Тесты» видны все попытки с датами и баллами.

3. У отзыва появляется ответ администратора — колонками, а не строкой треда:
   ответ у отзыва один, вторых уровней у него не бывает. Это закрывает
   `"reply": null` из сессии 3.

4. Мягкое удаление у отзыва и у сообщения треда — по тому же правилу actor_id.

Revision ID: b8e5127d4a03
Revises: f2a640c8b1d7
Create Date: 2026-08-19 17:30:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'b8e5127d4a03'
down_revision = 'f2a640c8b1d7'
branch_labels = None
depends_on = None

# Ровно те шесть, что лежали в domain/dictionaries.py, с теми же id.
CATEGORIES = [
    (1, 'Цифровые навыки'),
    (2, 'Методика преподавания'),
    (3, 'Оценивание'),
    (4, 'Инклюзивное образование'),
    (5, 'Классное руководство'),
    (6, 'Предметные курсы'),
]


def upgrade() -> None:
    op.create_table(
        'category',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('order_index', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_category')),
        sa.UniqueConstraint('title', name=op.f('uq_category_title')),
    )
    for number, (category_id, title) in enumerate(CATEGORIES, start=1):
        op.execute(
            sa.text(
                'INSERT INTO category (id, title, order_index) VALUES (:id, :title, :order_index)'
            ).bindparams(id=category_id, title=title, order_index=number)
        )
    # Последовательность id двигается вручную: строки вставлены с явными id,
    # и без этого первая созданная в админке категория упрётся в id = 1.
    op.execute("SELECT setval('category_id_seq', (SELECT max(id) FROM category))")

    op.add_column('quiz_attempt', sa.Column('uncounted_by', sa.BigInteger(), nullable=True))
    op.add_column('quiz_attempt', sa.Column('uncounted_reason', sa.Text(), nullable=True))
    op.add_column(
        'quiz_attempt', sa.Column('uncounted_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f('fk_quiz_attempt_uncounted_by_user'), 'quiz_attempt', 'user', ['uncounted_by'], ['id']
    )

    op.add_column('review', sa.Column('reply_text', sa.Text(), nullable=True))
    op.add_column('review', sa.Column('reply_by', sa.BigInteger(), nullable=True))
    op.add_column('review', sa.Column('reply_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('review', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('review', sa.Column('deleted_by', sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f('fk_review_reply_by_user'), 'review', 'user', ['reply_by'], ['id'])
    op.create_foreign_key(
        op.f('fk_review_deleted_by_user'), 'review', 'user', ['deleted_by'], ['id']
    )

    op.add_column(
        'thread_message', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column('thread_message', sa.Column('deleted_by', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f('fk_thread_message_deleted_by_user'), 'thread_message', 'user', ['deleted_by'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint(op.f('fk_thread_message_deleted_by_user'), 'thread_message', type_='foreignkey')
    op.drop_column('thread_message', 'deleted_by')
    op.drop_column('thread_message', 'deleted_at')
    op.drop_constraint(op.f('fk_review_deleted_by_user'), 'review', type_='foreignkey')
    op.drop_constraint(op.f('fk_review_reply_by_user'), 'review', type_='foreignkey')
    op.drop_column('review', 'deleted_by')
    op.drop_column('review', 'deleted_at')
    op.drop_column('review', 'reply_at')
    op.drop_column('review', 'reply_by')
    op.drop_column('review', 'reply_text')
    op.drop_constraint(op.f('fk_quiz_attempt_uncounted_by_user'), 'quiz_attempt', type_='foreignkey')
    op.drop_column('quiz_attempt', 'uncounted_at')
    op.drop_column('quiz_attempt', 'uncounted_reason')
    op.drop_column('quiz_attempt', 'uncounted_by')
    op.drop_table('category')

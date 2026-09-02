# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""platforms: platform column, keys and course_platform

Площадок становится две (PLATFORMS_BRIEF, решение владельца 03.09.2026).
Курс, аккаунт, админка и база остаются общими; раздельными становятся
доступ, прогресс, попытки, работы и сертификаты — и это значит, что
платформа входит в ключи, а не просто дописывается колонкой.

Три вещи одной ревизией:

1. Колонка `platform` в девяти таблицах. Существующим строкам проставляется
   код первой площадки — той, что работала до разделения. Заливка идёт
   через `server_default`, и он тут же снимается: новая строка обязана
   назвать платформу явно, иначе забытая платформа тихо становилась бы
   первой, а не падала.

2. Ключи. У доступа, прогресса, попытки, работы и сертификата платформа
   входит в уникальность: курс, выложенный на обеих площадках, на каждой
   изучается отдельно — свой доступ, свой прогресс, своя единственная
   попытка, свой сертификат. У заявки, отзыва, вопроса и уведомления
   платформа значит «откуда пришло» и в ключах не участвует.
   Номер сертификата остаётся уникальным на всю базу: платформы делят
   одну нумерацию, иначе проверка по номеру перестаёт быть однозначной.

3. Таблица `course_platform`: строка есть — курс выложен на этой площадке
   по этой цене. Заполняется по строке на каждый существующий курс с его
   нынешней ценой. `course.price` при этом остаётся на месте и продолжает
   читаться: колонку убирает следующая сессия, когда чтение переключится.
   Одним шагом каталог остался бы сломанным между сессиями, а он в бою.

Вниз эта ревизия не откатывается: у `lesson_progress` платформа входит
в первичный ключ, и снятие колонки схлопывало бы две строки одного урока
в одну. Чиним новой ревизией, а не откатом.

Revision ID: a1c8f5d3e920
Revises: c9d17a4e2b30
Create Date: 2026-09-03 11:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = 'a1c8f5d3e920'
down_revision = 'c9d17a4e2b30'
branch_labels = None
depends_on = None

# Девять таблиц получают колонку одинаково. Список тот же, что в брифе:
# первые пять — с участием в ключах, четыре последние — только колонкой.
PLATFORM_TABLES = (
    'enrollment',
    'lesson_progress',
    'quiz_attempt',
    'submission',
    'certificate',
    'lead',
    'review',
    'thread_message',
    'notification',
)

PLATFORM_CHECK = "platform IN ('p1', 'p2')"

# Код первой площадки — той, что работала до разделения (app/domain/platform.py).
# Здесь он написан строкой нарочно: миграция описывает состояние базы
# на свою дату и не должна меняться вместе с константой в коде.
FIRST_PLATFORM = 'p1'


def upgrade() -> None:
    for table in PLATFORM_TABLES:
        op.add_column(
            table,
            sa.Column('platform', sa.Text(), nullable=False, server_default=FIRST_PLATFORM),
        )
        # Умолчание жило ровно на время заливки существующих строк
        op.alter_column(table, 'platform', server_default=None)
        op.create_check_constraint(op.f(f'ck_{table}_platform'), table, PLATFORM_CHECK)

    # Доступ: тот же курс на второй площадке — это второй доступ
    op.drop_constraint(
        op.f('uq_enrollment_user_id_course_id'), 'enrollment', type_='unique'
    )
    op.create_unique_constraint(
        op.f('uq_enrollment_user_id_course_id_platform'),
        'enrollment',
        ['user_id', 'course_id', 'platform'],
    )

    # Прогресс: платформа в первичном ключе — урок общего курса проходится
    # на каждой площадке заново
    op.drop_constraint(op.f('pk_lesson_progress'), 'lesson_progress', type_='primary')
    op.create_primary_key(
        op.f('pk_lesson_progress'), 'lesson_progress', ['user_id', 'lesson_id', 'platform']
    )

    # Единственная зачётная и единственная активная попытка — теперь
    # единственные на площадке: купивший общий курс дважды получает
    # две попытки одного теста (PLATFORMS_BRIEF, решение 2)
    op.drop_index('uq_quiz_attempt_counted', table_name='quiz_attempt')
    op.create_index(
        'uq_quiz_attempt_counted',
        'quiz_attempt',
        ['user_id', 'quiz_id', 'platform'],
        unique=True,
        postgresql_where=sa.text('is_counted'),
    )
    op.drop_index('uq_quiz_attempt_active', table_name='quiz_attempt')
    op.create_index(
        'uq_quiz_attempt_active',
        'quiz_attempt',
        ['user_id', 'quiz_id', 'platform'],
        unique=True,
        postgresql_where=sa.text('finished_at IS NULL'),
    )

    # Одна работа «на проверке» — на площадку
    op.drop_index('uq_submission_pending', table_name='submission')
    op.create_index(
        'uq_submission_pending',
        'submission',
        ['user_id', 'task_id', 'platform'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Один действующий сертификат — на площадку. Уникальность номера
    # (uq_certificate_number) не трогаем: нумерация общая на всю базу
    op.drop_index('uq_certificate_active', table_name='certificate')
    op.create_index(
        'uq_certificate_active',
        'certificate',
        ['user_id', 'course_id', 'platform'],
        unique=True,
        postgresql_where=sa.text('revoked_at IS NULL'),
    )

    # Публикация и цена: строка есть — курс выложен на этой площадке.
    # Цена своя у каждой (PLATFORMS_BRIEF, решение 3), и она бывает пустой
    # ровно так же, как пустой бывает course.price
    op.create_table(
        'course_platform',
        sa.Column('course_id', sa.BigInteger(), nullable=False),
        sa.Column('platform', sa.Text(), nullable=False),
        sa.Column('price', sa.Integer(), nullable=True),
        sa.CheckConstraint(PLATFORM_CHECK, name=op.f('ck_course_platform_platform')),
        sa.ForeignKeyConstraint(
            ['course_id'], ['course.id'], name=op.f('fk_course_platform_course_id_course')
        ),
        sa.PrimaryKeyConstraint('course_id', 'platform', name=op.f('pk_course_platform')),
    )
    # Все нынешние курсы выложены на первой площадке по своей нынешней цене:
    # до разделения площадка была одна, и каталог обязан остаться прежним
    op.execute(
        sa.text(
            'INSERT INTO course_platform (course_id, platform, price)'
            ' SELECT id, :platform, price FROM course'
        ).bindparams(platform=FIRST_PLATFORM)
    )


def downgrade() -> None:
    # Платформа входит в первичный ключ lesson_progress и в четыре
    # уникальных индекса: снятие колонки схлопнуло бы прогресс, попытки
    # и сертификаты двух площадок в одну строку — то есть потеряло бы
    # чужую учёбу. Чиним новой ревизией (backend/CLAUDE.md).
    raise RuntimeError('Ревизия a1c8f5d3e920 необратима: вниз не откатываемся, чиним новой')

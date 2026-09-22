# Миграции вниз не откатываем — чиним новой (backend/CLAUDE.md).
"""curriculum tables

Справочник школьной учебной программы Казахстана (TEACHER_AI_BRIEF, раздел 3) —
ядро помощника учителя «Жоспар». Без него модель пишет «вообще про тему»,
а завуч проверяет план по коду цели обучения.

Пять таблиц одной ревизией: порознь ни одна из них не работает, а справочник
наполняется одним импортом приказа Министра просвещения РК № 399 от 16.09.2022.

1. `curriculum_subject` — предмет: слаг латиницей и служебное имя для админки.
2. `curriculum_program` — предмет + языковая редакция + приказ. Русская
   и казахская версии — две строки, а не перевод: в приказе это два
   официальных корпуса. Смена программы заведёт новую строку, старая
   останется — по ней уже сгенерированы документы.
3. `curriculum_section` — раздел и подраздел в одной таблице, два уровня
   через `parent_id`.
4. `learning_objective` — цель обучения: код строкой (`1.1.2.3` числом
   не бывает, ведущие нули значащие), текст, класс, четверти.
5. `curriculum_issue` — подозрение автопроверки импорта: по этому списку
   владелец вычитывает справочник (раздел 3.1).

Колонки `platform` нет ни в одной таблице, и это не забывчивость: справочник —
не наш контент, а государственная программа, одна на обе площадки (раздел 8).

Со старыми строками не происходит ничего: все пять таблиц новые, ни одна
существующая колонка не меняется и ни одна строка не переписывается.
Данными миграция не сеет — импорт приказа идёт отдельным кодом.

Revision ID: b9d3e04f71a2
Revises: f7b2e91a4c60
Create Date: 2026-09-19 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b9d3e04f71a2'
down_revision = 'f7b2e91a4c60'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'curriculum_subject',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('order_index', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_curriculum_subject')),
        sa.UniqueConstraint('code', name=op.f('uq_curriculum_subject_code')),
    )

    op.create_table(
        'curriculum_program',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('subject_id', sa.BigInteger(), nullable=False),
        sa.Column('lang', sa.Text(), nullable=False),
        sa.Column('grade_from', sa.Integer(), nullable=False),
        sa.Column('grade_to', sa.Integer(), nullable=False),
        sa.Column('order_number', sa.Text(), nullable=False),
        sa.Column('order_date', sa.Date(), nullable=False),
        # Сырая строка из шапки документа рядом с разобранной датой: формат
        # официальных документов плавает, и по ней видно, что разобрал импорт.
        sa.Column('order_date_text', sa.Text(), nullable=False),
        sa.Column('source_document_id', sa.Text(), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('revision_date', sa.Date(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), server_default='loaded', nullable=False),
        sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("lang IN ('ru', 'kz')", name=op.f('ck_curriculum_program_lang')),
        # Загружен и опубликован — два состояния справочника: генерация видит
        # только опубликованный.
        sa.CheckConstraint("status IN ('loaded', 'published')", name=op.f('ck_curriculum_program_status')),
        sa.ForeignKeyConstraint(
            ['subject_id'], ['curriculum_subject.id'],
            name=op.f('fk_curriculum_program_subject_id_curriculum_subject'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_curriculum_program')),
        # Одна действующая программа на предмет и язык: повторный импорт того же
        # приказа не должен молча удвоить справочник.
        sa.UniqueConstraint(
            'subject_id', 'lang', 'order_number',
            name=op.f('uq_curriculum_program_subject_id_lang_order_number'),
        ),
    )

    op.create_table(
        'curriculum_section',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('program_id', sa.BigInteger(), nullable=False),
        # Раздел — строка с пустым parent_id, подраздел ссылается на свой раздел.
        sa.Column('parent_id', sa.BigInteger(), nullable=True),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('order_index', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(
            ['parent_id'], ['curriculum_section.id'],
            name=op.f('fk_curriculum_section_parent_id_curriculum_section'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['program_id'], ['curriculum_program.id'],
            name=op.f('fk_curriculum_section_program_id_curriculum_program'), ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_curriculum_section')),
        sa.UniqueConstraint('program_id', 'code', name=op.f('uq_curriculum_section_program_id_code')),
    )
    op.create_index(op.f('ix_curriculum_section_program_id'), 'curriculum_section', ['program_id'])

    op.create_table(
        'learning_objective',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('program_id', sa.BigInteger(), nullable=False),
        # Ссылка на подраздел, а не на раздел: в приказе цели разложены
        # по подразделам, раздел достаётся по parent_id подраздела.
        sa.Column('section_id', sa.BigInteger(), nullable=False),
        # Код строкой: 1.1.2.3 числом не бывает, ведущие нули значащие.
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('grade', sa.Integer(), nullable=False),
        sa.Column('index', sa.Integer(), nullable=False),
        sa.Column('marker', sa.Text(), server_default='', nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        # Одна цель изучается в нескольких четвертях — отсюда массив, а не число.
        sa.Column('quarters', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False),
        sa.Column('has_image', sa.Boolean(), server_default='false', nullable=False),
        sa.CheckConstraint('grade BETWEEN 1 AND 12', name=op.f('ck_learning_objective_grade')),
        sa.ForeignKeyConstraint(
            ['program_id'], ['curriculum_program.id'],
            name=op.f('fk_learning_objective_program_id_curriculum_program'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['section_id'], ['curriculum_section.id'],
            name=op.f('fk_learning_objective_section_id_curriculum_section'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_learning_objective')),
        sa.UniqueConstraint('program_id', 'code', name=op.f('uq_learning_objective_program_id_code')),
    )
    op.create_index(op.f('ix_learning_objective_program_id'), 'learning_objective', ['program_id'])
    op.create_index(op.f('ix_learning_objective_section_id'), 'learning_objective', ['section_id'])

    op.create_table(
        'curriculum_issue',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('program_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('severity', sa.Text(), nullable=False),
        # Код целью, а не ссылкой: подозрение бывает ровно о той цели, которой
        # в этой редакции нет — её потеряли при разборе, и ссылаться не на что.
        sa.Column('objective_code', sa.Text(), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        # Строка гасится, а не удаляется: иначе повторный импорт показал бы
        # разобранное подозрение во второй раз.
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.BigInteger(), nullable=True),
        sa.CheckConstraint("severity IN ('error', 'warning')", name=op.f('ck_curriculum_issue_severity')),
        sa.ForeignKeyConstraint(
            ['program_id'], ['curriculum_program.id'],
            name=op.f('fk_curriculum_issue_program_id_curriculum_program'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['resolved_by'], ['user.id'], name=op.f('fk_curriculum_issue_resolved_by_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_curriculum_issue')),
    )
    op.create_index(op.f('ix_curriculum_issue_program_id'), 'curriculum_issue', ['program_id'])


def downgrade() -> None:
    # Редкий случай обратимой правки: таблицы новые и пустые, за ними ничего
    # не стоит — откат не делает вид, что вернул данные, которых больше нет.
    op.drop_table('curriculum_issue')
    op.drop_table('learning_objective')
    op.drop_table('curriculum_section')
    op.drop_table('curriculum_program')
    op.drop_table('curriculum_subject')

"""Classes, class timetable and teacher-hosted sessions

* school_classes: year groups / programmes (Year 8, Bridging, ...).
* student_profiles.class_id: which class a student is in.
* meetings.class_id / subject / duration_minutes / series_id: a timetabled
  lesson is for one class, one subject, with a length, and lessons created by
  "repeat weekly" share a series id. meetings.host_id may now be a teacher.
* meeting_invites.source: "roster" (kept in step with placements) or "manual".

Default classes are seeded on app start (school.seed_default_classes), which
also covers databases that Alembic never ran against.

Revision ID: d4a7e2b9f315
Revises: c3f1d8a2e611
Create Date: 2026-11-12 08:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4a7e2b9f315'
down_revision = 'c3f1d8a2e611'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'school_classes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_school_classes_name'),
    )

    with op.batch_alter_table('student_profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('class_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_student_profiles_class_id', ['class_id'], unique=False)
        batch_op.create_foreign_key('fk_student_profiles_class_id', 'school_classes', ['class_id'], ['id'])

    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('class_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('subject', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('duration_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('series_id', sa.String(length=16), nullable=True))
        batch_op.create_index('ix_meetings_class_id', ['class_id'], unique=False)
        batch_op.create_index('ix_meetings_series_id', ['series_id'], unique=False)
        batch_op.create_foreign_key('fk_meetings_class_id', 'school_classes', ['class_id'], ['id'])

    with op.batch_alter_table('meeting_invites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=10), nullable=False, server_default='manual'))


def downgrade():
    with op.batch_alter_table('meeting_invites', schema=None) as batch_op:
        batch_op.drop_column('source')

    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_constraint('fk_meetings_class_id', type_='foreignkey')
        batch_op.drop_index('ix_meetings_series_id')
        batch_op.drop_index('ix_meetings_class_id')
        batch_op.drop_column('series_id')
        batch_op.drop_column('duration_minutes')
        batch_op.drop_column('subject')
        batch_op.drop_column('class_id')

    with op.batch_alter_table('student_profiles', schema=None) as batch_op:
        batch_op.drop_constraint('fk_student_profiles_class_id', type_='foreignkey')
        batch_op.drop_index('ix_student_profiles_class_id')
        batch_op.drop_column('class_id')

    op.drop_table('school_classes')

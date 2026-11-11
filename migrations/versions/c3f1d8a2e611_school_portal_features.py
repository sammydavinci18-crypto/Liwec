"""School portal: application reference numbers, announcements, progress reports

Revision ID: c3f1d8a2e611
Revises: b7e4a91c5d20
Create Date: 2026-11-11 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3f1d8a2e611'
down_revision = 'b7e4a91c5d20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('student_profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reference_number', sa.String(length=20), nullable=True))
        batch_op.create_index('ix_student_profiles_reference_number', ['reference_number'], unique=False)

    # Number existing applications (the app also does this lazily if skipped).
    op.execute(
        "UPDATE student_profiles SET reference_number = "
        "'LW-' || CAST(EXTRACT(YEAR FROM COALESCE(submitted_at, CURRENT_TIMESTAMP)) AS VARCHAR) "
        "|| '-' || LPAD(CAST(id AS VARCHAR), 4, '0') WHERE reference_number IS NULL"
    ) if op.get_bind().dialect.name == "postgresql" else None

    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('audience', sa.String(length=20), nullable=False),
        sa.Column('event_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'progress_reports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=False),
        sa.Column('term', sa.String(length=60), nullable=False),
        sa.Column('subject', sa.String(length=120), nullable=False),
        sa.Column('grade', sa.String(length=30), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('strengths', sa.Text(), nullable=True),
        sa.Column('next_steps', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.ForeignKeyConstraint(['teacher_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('progress_reports', schema=None) as batch_op:
        batch_op.create_index('ix_progress_reports_student_id', ['student_id'], unique=False)


def downgrade():
    op.drop_table('progress_reports')
    op.drop_table('announcements')
    with op.batch_alter_table('student_profiles', schema=None) as batch_op:
        batch_op.drop_index('ix_student_profiles_reference_number')
        batch_op.drop_column('reference_number')

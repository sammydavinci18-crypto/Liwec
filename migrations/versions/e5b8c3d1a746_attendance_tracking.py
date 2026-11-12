"""Attendance tracking for compulsory lessons

meeting_participants.away_count / away_seconds: how often, and for how long in
total, a student left the lesson screen during a teacher-led lesson.

Revision ID: e5b8c3d1a746
Revises: d4a7e2b9f315
Create Date: 2026-11-12 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e5b8c3d1a746'
down_revision = 'd4a7e2b9f315'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('meeting_participants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('away_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('away_seconds', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('meeting_participants', schema=None) as batch_op:
        batch_op.drop_column('away_seconds')
        batch_op.drop_column('away_count')

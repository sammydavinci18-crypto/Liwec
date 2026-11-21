"""Shareable student links (guests without accounts)

meetings.guest_token / guest_link_open / guest_auto_admit: the lesson's share
link and how it behaves. meeting_guests: one row per person who came in through
the link (name + attendance). Purely additive; nothing is dropped.

Revision ID: f6c9d2e4b857
Revises: e5b8c3d1a746
Create Date: 2026-11-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f6c9d2e4b857'
down_revision = 'e5b8c3d1a746'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('guest_token', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('guest_link_open', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('guest_auto_admit', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.create_index('ix_meetings_guest_token', ['guest_token'], unique=True)

    op.create_table(
        'meeting_guests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('entered_at', sa.DateTime(), nullable=True),
        sa.Column('left_at', sa.DateTime(), nullable=True),
        sa.Column('away_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('away_seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('removed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_guests_meeting_id', 'meeting_guests', ['meeting_id'], unique=False)


def downgrade():
    op.drop_index('ix_meeting_guests_meeting_id', table_name='meeting_guests')
    op.drop_table('meeting_guests')
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_index('ix_meetings_guest_token')
        batch_op.drop_column('guest_auto_admit')
        batch_op.drop_column('guest_link_open')
        batch_op.drop_column('guest_token')

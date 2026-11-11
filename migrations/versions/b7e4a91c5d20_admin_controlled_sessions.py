"""Admin-controlled sessions: invites, requests, meeting types

Every meeting is now created by the admin, with a guest list (meeting_invites).
People ask for consultations/meetings through meeting_requests, which the admin
approves or declines. The old consultant-marketplace tables (consultant_profiles,
availability_slots, appointments, conversations, ...) are intentionally NOT
dropped here, so no historical data is destroyed; the application simply no
longer uses them and an admin can drop them by hand later if wanted.

Revision ID: b7e4a91c5d20
Revises: 28ab73a29f16
Create Date: 2026-11-11 09:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4a91c5d20'
down_revision = '28ab73a29f16'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('meeting_type', sa.String(length=20), nullable=False, server_default='consultation'))
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))

    op.create_table(
        'meeting_invites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('invited_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'user_id', name='uq_meeting_invite'),
    )

    op.create_table(
        'meeting_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('requester_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('topic', sa.String(length=200), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('preferred_time', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('admin_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by_id', sa.Integer(), nullable=True),
        sa.Column('meeting_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['decided_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id']),
        sa.ForeignKeyConstraint(['requester_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # Sessions that existed before this change were created without a guest
    # list. Give each one's previous participants an invite, so existing
    # recordings stay watchable by the people who were actually in them.
    op.execute(
        "INSERT INTO meeting_invites (meeting_id, user_id, invited_at) "
        "SELECT DISTINCT p.meeting_id, p.user_id, CURRENT_TIMESTAMP "
        "FROM meeting_participants p "
        "JOIN meetings m ON m.id = p.meeting_id "
        "WHERE p.user_id <> m.host_id"
    )


def downgrade():
    op.drop_table('meeting_requests')
    op.drop_table('meeting_invites')
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_column('description')
        batch_op.drop_column('meeting_type')

"""Recording share links, save note and size

Adds four columns to recordings. Purely additive; nothing is dropped.

Revision ID: c8f2a1d7e405
Revises: b9e2c4a6d013
Create Date: 2026-11-24 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c8f2a1d7e405'
down_revision = 'b9e2c4a6d013'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recordings') as batch:
        batch.add_column(sa.Column('share_token', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('share_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column('save_note', sa.Text(), nullable=True))
        batch.add_column(sa.Column('size_bytes', sa.BigInteger(), nullable=True))
        batch.create_index('ix_recordings_share_token', ['share_token'])


def downgrade():
    with op.batch_alter_table('recordings') as batch:
        batch.drop_index('ix_recordings_share_token')
        batch.drop_column('size_bytes')
        batch.drop_column('save_note')
        batch.drop_column('share_enabled')
        batch.drop_column('share_token')

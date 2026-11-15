"""Versioned curation, typed processing flags and field-level evidence."""
from pathlib import Path
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    raise RuntimeError('Curation evidence is retained; restore a backup to downgrade.')

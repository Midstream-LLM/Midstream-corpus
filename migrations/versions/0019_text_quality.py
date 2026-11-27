"""Per-text missing-glyph counts used to choose which duplicate to keep."""
from pathlib import Path
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.get_bind().exec_driver_sql('DROP TABLE text_quality')

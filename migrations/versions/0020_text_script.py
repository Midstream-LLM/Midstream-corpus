"""Per-text Han and traditional-character counts used to keep both scripts of a duplicate."""
from pathlib import Path
from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.get_bind().exec_driver_sql('DROP TABLE text_script')

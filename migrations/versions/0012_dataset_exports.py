"""同一发布的多次导出及分片记录。"""
from pathlib import Path
from alembic import op
revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    for name in ('export_id', 'split', 'row_count', 'size_bytes'):
        op.drop_column('export_artifact', name)
    op.drop_table('dataset_export')

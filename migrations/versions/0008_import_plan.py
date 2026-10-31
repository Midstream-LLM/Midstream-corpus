"""批次固定声明式导入方案。"""
from pathlib import Path
from alembic import op
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_column('source_batch', 'import_plan')

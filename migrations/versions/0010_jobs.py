"""持久任务与事件日志。"""
from pathlib import Path
from alembic import op
revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_table('job_event')
    op.drop_table('job')

"""发布固定原文字符裁剪区间。"""
from pathlib import Path
from alembic import op
revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_table('dataset_member_cut')

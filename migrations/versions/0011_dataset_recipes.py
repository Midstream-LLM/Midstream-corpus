"""保存数据集配方。"""
from pathlib import Path
from alembic import op
revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_table('dataset_recipe')

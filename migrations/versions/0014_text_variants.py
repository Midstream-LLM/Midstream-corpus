"""不可变派生版本；同一主线输入只有一个同类有效派生版本。"""
from pathlib import Path
from alembic import op
revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

def upgrade():
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix('.sql').read_text())

def downgrade():
    op.drop_index('one_variant_per_input')
    op.drop_constraint('variant_is_not_current', 'document_version')
    op.drop_column('document_version', 'variant')

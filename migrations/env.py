from alembic import context
from sqlalchemy import create_engine

from midstream.config import alembic_url

engine = create_engine(alembic_url())
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()

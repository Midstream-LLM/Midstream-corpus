"""配方草稿的增删改查。"""

from psycopg.types.json import Jsonb


def list_recipes(conn):
    return [row[0] for row in conn.execute('SELECT to_jsonb(r) FROM dataset_recipe r ORDER BY name')]


def get_recipe(conn, name):
    from ..corpus.management import ManagementError
    row = conn.execute('SELECT to_jsonb(r) FROM dataset_recipe r WHERE name = %s', (name,)).fetchone()
    if row is None:
        raise ManagementError('not_found', f'配方 {name} 不存在')
    return row[0]


def save_recipe(conn, recipe, *, previous=None):
    from ..corpus.management import ManagementError
    if previous is None:
        row = conn.execute('INSERT INTO dataset_recipe (name, body) VALUES (%s, %s)'
                           ' ON CONFLICT DO NOTHING RETURNING name', (recipe.name, Jsonb(recipe.raw))).fetchone()
    else:
        row = conn.execute('UPDATE dataset_recipe SET name = %s, body = %s, updated_at = now()'
                           ' WHERE name = %s RETURNING name', (recipe.name, Jsonb(recipe.raw), previous)).fetchone()
    if row is None:
        raise ManagementError('conflict', '配方已存在或待修改配方不存在')
    conn.commit()
    return get_recipe(conn, recipe.name)


def delete_recipe(conn, name):
    from ..corpus.management import ManagementError
    if not conn.execute('DELETE FROM dataset_recipe WHERE name = %s RETURNING name', (name,)).fetchone():
        raise ManagementError('not_found', f'配方 {name} 不存在')
    conn.commit()
    return {'name': name, 'deleted': True}

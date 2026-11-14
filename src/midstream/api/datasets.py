"""数据集试算、发布、导出、token 统计与配方。"""

import json

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated

from .. import jobs
from ..corpus import management
from ..datasets.recipe import Recipe
from ..datasets.recipes import delete_recipe, get_recipe, list_recipes, save_recipe
from ..datasets.release import get_release, list_releases
from ..datasets.selection import facets
from ..delivery import token_stats, tokenizing
from ..delivery.export import list_exports, Options
from .deps import Operation, browser_write, connection, write_connection

router = APIRouter()


class DatasetPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipe: dict
    tokenizer: str | None = None


class DatasetCreate(Operation):
    recipe: dict


class DatasetExport(Operation):
    options: Options = Field(default_factory=Options)


class TokenStatsRequest(Operation):
    tokenizer: str = Field(min_length=1, max_length=4096)


class DatasetVerify(Operation):
    hash_files: bool = False


class TrainingRegister(Operation):
    run_name: str = Field(min_length=1, max_length=200, pattern=r'\S')


@router.get('/api/v1/datasets/facets')
def dataset_facets(filters: str = '{}', recipe: str | None = None, conn=Depends(connection)):
    body = json.loads(recipe) if recipe else {
        'name': 'facets', 'seed': '', 'splits': {'train': 1}, 'filters': json.loads(filters)}
    return facets(conn, Recipe.load(body))


@router.post('/api/v1/datasets/preview')
def dataset_preview(body: DatasetPreview, conn=Depends(connection)):
    tokenizer = tokenizing.load(body.tokenizer) if body.tokenizer is not None else None
    return token_stats.plan_with_estimate(conn, Recipe.load(body.recipe), tokenizer).report


@router.post('/api/v1/datasets', dependencies=[Depends(browser_write)])
def dataset_create(body: DatasetCreate, conn=Depends(write_connection)):
    return jobs.submit(conn, 'dataset_create', {'recipe': body.recipe}, body.actor, body.reason)


@router.get('/api/v1/datasets')
def dataset_list(before: Annotated[int | None, Query(gt=0)] = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 30, conn=Depends(connection)):
    rows = list_releases(conn, limit=limit+1, before=before)
    return {'items': rows[:limit], 'next_cursor': rows[limit-1]['release_id'] if len(rows)>limit else None}


@router.get('/api/v1/datasets/{release_id}')
def dataset_detail(release_id: int, conn=Depends(connection)):
    return get_release(conn, release_id)


@router.post('/api/v1/datasets/{release_id}/exports', dependencies=[Depends(browser_write)])
def dataset_export(release_id: int, body: DatasetExport, conn=Depends(write_connection)):
    get_release(conn, release_id)
    return jobs.submit(conn, 'dataset_export', {'release_id': release_id, 'options': body.options.model_dump()},
                       body.actor, body.reason)


@router.get('/api/v1/datasets/{release_id}/exports')
def dataset_exports(release_id: int, conn=Depends(connection)):
    get_release(conn, release_id)
    return list_exports(conn, release_id)


@router.post('/api/v1/datasets/{release_id}/token-stats', dependencies=[Depends(browser_write)])
def dataset_token_stats_create(release_id: int, body: TokenStatsRequest, conn=Depends(write_connection)):
    get_release(conn, release_id)
    return jobs.submit(conn, 'token_stats', {'release_id': release_id, 'tokenizer': body.tokenizer},
                       body.actor, body.reason)


@router.get('/api/v1/datasets/{release_id}/token-stats')
def dataset_token_stats(release_id: int, conn=Depends(connection)):
    return token_stats.list_stats(conn, release_id)


@router.post('/api/v1/datasets/{release_id}/verify', dependencies=[Depends(browser_write)])
def dataset_verify(release_id: int, body: DatasetVerify, conn=Depends(write_connection)):
    get_release(conn, release_id)
    return jobs.submit(conn, 'dataset_verify', {'release_id': release_id, 'hash_files': body.hash_files},
                       body.actor, body.reason)


@router.post('/api/v1/datasets/{release_id}/training', dependencies=[Depends(browser_write)])
def dataset_training(release_id: int, body: TrainingRegister, conn=Depends(write_connection)):
    management._identity(body.actor, body.reason)
    get_release(conn, release_id)
    conn.execute('INSERT INTO training_use (release_id,run_name,used_at,note) VALUES (%s,%s,now(),%s)',
                 (release_id, body.run_name, json.dumps({'actor': body.actor, 'reason': body.reason}, ensure_ascii=False)))
    conn.commit()
    return {'release_id': release_id, 'run_name': body.run_name}


@router.get('/api/v1/datasets/{release_id}/training')
def dataset_training_list(release_id: int, conn=Depends(connection)):
    get_release(conn, release_id)
    return [row[0] for row in conn.execute('SELECT to_jsonb(t) FROM training_use t WHERE release_id=%s'
                                          ' ORDER BY use_id DESC', (release_id,))]


@router.get('/api/v1/dataset-recipes')
def recipe_list(conn=Depends(connection)):
    return list_recipes(conn)


@router.get('/api/v1/dataset-recipes/{name}')
def recipe_get(name: str, conn=Depends(connection)):
    return get_recipe(conn, name)


@router.post('/api/v1/dataset-recipes', dependencies=[Depends(browser_write)])
def recipe_create(body: DatasetCreate, conn=Depends(write_connection)):
    management._identity(body.actor, body.reason)
    return save_recipe(conn, Recipe.load(body.recipe))


@router.put('/api/v1/dataset-recipes/{name}', dependencies=[Depends(browser_write)])
def recipe_update(name: str, body: DatasetCreate, conn=Depends(write_connection)):
    management._identity(body.actor, body.reason)
    return save_recipe(conn, Recipe.load(body.recipe), previous=name)


@router.delete('/api/v1/dataset-recipes/{name}', dependencies=[Depends(browser_write)])
def recipe_delete(name: str, body: Operation, conn=Depends(write_connection)):
    management._identity(body.actor, body.reason)
    return delete_recipe(conn, name)

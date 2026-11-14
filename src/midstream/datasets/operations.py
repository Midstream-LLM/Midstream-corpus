"""数据集操作：固定成员。"""

from pydantic import Field, field_validator

from ..operations import Context, Params, operation
from .plan import plan_release
from .recipe import ExportError, Recipe
from .release import write_release


class DatasetCreateParams(Params):
    recipe: dict

    @field_validator('recipe')
    @classmethod
    def loadable(cls, value):
        Recipe.load(value)
        return value


class ReleaseParams(Params):
    release_id: int = Field(gt=0)


@operation('dataset_create', '按配方试算并固定数据集成员')
def _dataset_create(conn, params: DatasetCreateParams, ctx: Context):
    recipe = Recipe.load(params.recipe)
    existing = conn.execute('SELECT release_id, recipe FROM dataset_release WHERE name = %s',
                            (recipe.name,)).fetchone()
    if existing:
        if existing[1] != recipe.raw:
            raise ExportError('已有同名数据集，配方不同')
        return {'release_id': existing[0]}
    ctx.progress(0, 2, '试算选用与划分')
    plan = plan_release(conn, recipe)
    ctx.progress(1, 2, '固定数据集成员')
    return {'release_id': write_release(conn, plan), 'plan': plan.report}

"""LLM 接入点与配置。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..llm import deepseek, qwen
from ..llm import settings as llm_settings
from .deps import browser_write

router = APIRouter()


class ProviderModels(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credential: str
    api_key: str | None = None


class QwenModels(ProviderModels):
    region: str
    workspace: str


class LLMSettingsSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: dict
    keys: dict[str, str] = Field(default_factory=dict)
    clear_keys: list[str] = Field(default_factory=list)
    revision: str


@router.get('/api/v1/llm/deepseek')
def deepseek_preset():
    return deepseek.preset()


@router.post('/api/v1/llm/deepseek/models', dependencies=[Depends(browser_write)])
def deepseek_models(body: ProviderModels):
    return deepseek.models(body.credential, body.api_key)


@router.get('/api/v1/llm/qwen')
def qwen_preset():
    return qwen.preset()


@router.post('/api/v1/llm/qwen/models', dependencies=[Depends(browser_write)])
def qwen_models(body: QwenModels):
    return qwen.models(body.region, body.workspace, body.credential, body.api_key)


@router.get('/api/v1/llm/settings')
def llm_settings_get():
    return llm_settings.get()


@router.post('/api/v1/llm/settings', dependencies=[Depends(browser_write)])
def llm_settings_save(body: LLMSettingsSave):
    return llm_settings.save(body.config, body.keys, body.clear_keys, body.revision)

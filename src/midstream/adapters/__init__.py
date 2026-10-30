"""已注册的来源适配器。"""

from .base import Adapter
from .ccc import CccCorpusAdapter
from .wanli import WanliAdapter

ADAPTERS: dict[str, Adapter] = {
    adapter.name: adapter for adapter in (CccCorpusAdapter(), WanliAdapter())
}

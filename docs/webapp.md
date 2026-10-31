# Web app 第一版

前端为 React + TypeScript + Vite，后端为 FastAPI，直接调用现有 Python core。浏览器与未来 SwiftUI 客户端共用 `/api/v1`；未来 macOS App 可启动同一 Python 后端及私有 PostgreSQL，并连接 loopback。目前未实现 macOS 打包或进程托管。

## 已提供

- 文献列表、游标分页；标题前缀、作者、来源、部类、质量与成书年份筛选。
- 文档详情、历史版本列表和切换；保留原文空白、标点及异体字。
- 正文每段最多 20,000 字符，UI 每次读取 10,000 字符。当前后端仍需解压整篇后切片，超大单篇的解压优化待做。
- 加载、空结果、连接失败与重试状态；桌面双栏，窄屏上下排列。
- 第一版为只读浏览，人工审核、批次操作、数据集制作及 LLM 面板后续接入。

## 运行

在代码仓库根目录、已初始化好数据库的环境中：

```bash
source .venv/bin/activate
pip install -r requirements.txt
npm --prefix web ci
npm --prefix web run build
python -m midstream.api --port 8765
```

打开 `http://127.0.0.1:8765`。默认使用现有私有 PostgreSQL；可用 `MIDSTREAM_DSN` 指向其他实例。构建后由 Python 托管 `web/dist`，运行阶段不需要 Node 服务。安装到其他位置时可用 `--static-dir /path/to/web/dist` 指定前端构建目录。

Linux 部署时可从客户端建立 SSH 隧道，将本机端口转发到服务器的 `127.0.0.1:8765` 后访问。默认只监听 loopback，当前没有登录或多用户权限系统。此次开发没有部署到正式服务器。

开发时，先运行 API，再运行 `npm --prefix web run dev`；Vite 将 `/api` 代理到 `127.0.0.1:8765`。前端不存数据库地址或 LLM key。

## HTTP 接口

- `GET /api/v1/health`：服务版本和存活状态，不代表数据库可用。
- `GET /api/v1/sources`：来源名列表。
- `GET /api/v1/documents`：筛选当前版本，返回 `items` 和 `next_cursor`；下页传 `after`。默认每页 30，最多 100。
- `GET /api/v1/documents/{doc_id}/versions`：全部历史版本摘要，倒序。
- `GET /api/v1/documents/{doc_id}?version=2&offset=0&length=10000`：指定版本及正文分段，不传 version 时取当前版。
- `/api/docs` 和 `/api/openapi.json`：接口文档。

年代筛选沿用 core 的成书年份区间相交规则；未记录年代的文档不会命中指定年份区间。未知元数据显示为未知。正文按 Python Unicode 码点切片；API 会返回 `next_offset`，无后续文本时为 null。长文本调用方宜固定 version，避免多次请求之间当前版本变化。

数据库连接使用只读事务，每次请求后释放。数据库不可用返回 503，查不到文档或版本返回 404，参数无效返回 422。当前无全文检索，标题搜索仅为前缀匹配。

## 验证

本地独立 PostgreSQL 17 合成库中 63 个 Python 测试通过，包含真实版本读取与分页；前端 TypeScript 检查和生产构建通过。实际浏览器验证了目录、空筛选结果、文档阅读、版本切换及桌面/窄屏布局。尚未在正式库上验证性能。

使用 FastAPI（MIT）、Uvicorn（BSD-3-Clause）、React/Vite（MIT）、TypeScript（Apache-2.0）。构建依赖在 `web/package-lock.json` 中固定。

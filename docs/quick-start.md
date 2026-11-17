# Quick Start

使用 Docker Compose 启动数据库、记忆核心、MCP 和管理页面。

## 准备环境

安装 Docker Engine、Docker Compose 2.20.2+ 和 Python 3。后续容器运行使用 Python 3.12；记忆模型和向量模型服务需要单独准备。

## 模型接口

| 配置 | 支持的接口 |
|---|---|
| `MLLM_PROTOCOL=llamacpp` | 模型服务的 `/v1/chat/completions`，需要支持项目使用的结构化输出；处理多媒体还需要对应模态能力 |
| `MLLM_PROTOCOL=gateway` | 项目的结构化任务网关 `/tasks` |
| `EMB_BACKEND=vllm_gemma` | 已部署的 EmbeddingGemma 2 vLLM 服务，配置实际模型名称与不可变权重版本 |
| `EMB_BACKEND=http` | 项目向量网关 `/capabilities` 与 `/embed`；普通 OpenAI 风格 embeddings 地址不能直接替代此接口 |

容器中的 `127.0.0.1` 指容器自己。模型在 Docker 宿主机时可使用 `host.docker.internal`，Compose 已配置 Linux host gateway。模型服务必须监听容器可到达的接口，防火墙也要允许访问。k3s 应改用 Pod 可访问的 Service DNS 或内网地址。

## 启动 Docker Compose

在仓库根目录执行：

```bash
python3 ops/bootstrap.py
```

脚本创建权限为 `0600` 的 `.env`，生成互不相同的数据库密码、管理 token 和模型客户端 token；已有文件不会被覆盖。用本机编辑器打开 `.env`，填写以下项目：

- `MLLM_BASE_URL`、`MLLM_MODEL_NAME`，以及服务需要的 `MLLM_API_KEY`。
- `EMB_BASE_URL`、`EMB_MODEL_NAME`、`EMB_MODEL_REVISION` 和服务需要的 `EMB_API_KEY`。
- 如使用不同模型后端，调整 `MLLM_PROTOCOL` 或 `EMB_BACKEND`。

保留自动生成的 URL-safe 凭据，Compose 从 `.env` 读取部署配置。

```bash
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 1200
```

启动顺序为 PostgreSQL 健康 → 仓库初始化成功 → core 就绪 → MCP 和采集服务就绪。首次构建需要下载镜像和依赖，命令等待有上限，不重复创建导入任务。依赖条件使用 [Compose 启动顺序机制](https://docs.docker.com/compose/how-tos/startup-order/)。

## 登录与首次导入

打开 [管理页面](http://127.0.0.1:5073/manage/imports)，使用 `.env` 中的 `MEMU_OWNER_TOKEN` 登录。

1. 添加文件、Notion、飞书、Immich 或其他文档中列出的来源，填写自己的服务地址与授权凭据。
2. 浏览和预览内容，选择明确的文件、文档、会话或日期范围，再开始导入。
3. 在导入记录中查看结果；在记忆管理中查看 Unit、Source、Summary 和记忆主题。
4. 编辑保存会保护手动内容，相关索引与汇总在后台更新。

本地文件目录必须能被采集容器访问。按需要创建 `compose.override.yaml`，仅挂载准备导入的目录：

```yaml
services:
  memu-acquisition:
    volumes:
      - /absolute/path/to/selected-documents:/imports/documents:ro
```

重新执行 `docker compose up -d`，在页面填写容器路径 `/imports/documents`。来源目录不需要挂载给 MCP；原件经采集服务交付给核心。

## 连接 MCP

客户端选择 Streamable HTTP，地址为 `http://127.0.0.1:5071/mcp`，请求头使用：

```text
Authorization: Bearer <MEMU_AGENT_TOKEN 的实际值>
```

工具调用的 `from` 使用 `agent1`。完成首次导入和后台整理后，可调用 `memory_read` 读取 `MEMORY`；空仓库可能尚未生成这个入口。修订前先读目标的 `edit_revision`，协议见[记忆修订说明](memory-revisions.md)。

配置结构示例见 [MCP HTTP 示例](../examples/mcp-http.json)。客户端的配置字段可能不同，以客户端支持的 Streamable HTTP 设置为准。

## 状态与停止

```bash
docker compose ps
docker compose logs --tail=80 memu-core memu-acquisition memu-mcp
# 保留数据卷停止：
docker compose down
```

日常状态查询至少间隔 150 秒，优先使用命令完成通知。`/readyz` 检查服务与依赖就绪，不调用模型；模型地址、权限和效果仍需通过一次自己授权的实际导入确认。

`docker compose down` 保留数据卷。`docker compose down -v` 会删除该 Compose 项目的数据库、记忆与采集卷，只在明确要清空这套部署时使用。

完整的 k3s、反向代理和迁移流程见[部署指南](../deploy/README.md)。

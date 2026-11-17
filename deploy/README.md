# 部署与迁移

标准部署包含 PostgreSQL/pgvector、单写入 core、MCP 前端和采集/管理服务。模型独立部署。首次本机启动见 [Quick Start](../docs/quick-start.md)。

## Docker Compose

根目录 [compose.yaml](../compose.yaml) 提供完整启动依赖、健康检查和三个持久卷；数据库没有宿主端口，核心也不对外发布端口。

```bash
python3 ops/bootstrap.py
# 编辑 .env，配置模型接口。
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 1200
```

管理页为 `http://127.0.0.1:5073/manage/imports`，MCP 为 `http://127.0.0.1:5071/mcp`。Compose 的 `.env` 是模板插值输入，应用只接收对应角色的环境变量，避免把模型或数据库密钥交给 MCP。

| 镜像构建目标 | 命令 | 持久卷 |
|---|---|---|
| `core` | `init` / `core` | `/data/memory`，与数据库配套 |
| `mcp` | `mcp` | 无 |
| `acquisition` | `acquisition` | `/data/acquisition`，保存来源凭据、OAuth 和交付缓存 |
| `core-local-embedding` | `core` | 另需只读模型权重卷，属于可选高级部署 |

核心与采集服务各保持一个实例。core 退出时有 900 秒宽限期，提交中的任务由恢复协议处理；不要通过横向增加 core 副本共享同一文件仓库。MCP 无状态，可单独扩容。

## k3s

[k8s/base](k8s/base) 包含三类应用、PostgreSQL StatefulSet、Service、PVC 和核心/数据库入口 NetworkPolicy。需要集群有默认 StorageClass；k3s 默认提供 Local Path Provisioner。它使用节点本地磁盘，默认配置不提供跨节点数据高可用。[k3s 存储说明](https://docs.k3s.io/add-ons/storage)

### 1. 构建并交付镜像

```bash
docker build --target core -t memulife-core:1.3.0 .
docker build --target mcp -t memulife-mcp:1.3.0 .
docker build --target acquisition -t memulife-acquisition:1.3.0 .
```

在单节点 k3s 宿主机上可导入镜像：

```bash
docker save memulife-core:1.3.0 memulife-mcp:1.3.0 memulife-acquisition:1.3.0 -o /tmp/memulife-images.tar
sudo k3s ctr images import /tmp/memulife-images.tar
```

多节点需要推送到节点可访问的私有镜像仓库，并在 Kustomization 的 `images` 中替换名称；不能只把镜像导入一个节点却让 Pod 调度到其他节点。[k3s 镜像导入](https://docs.k3s.io/add-ons/import-images)

### 2. 生成私有配置

```bash
python3 ops/bootstrap.py
# 编辑 .env：模型地址改为 Pod 可达地址；填写自己的模型名称和版本。
mkdir -p .local
# 使用已构建镜像中的 Python 依赖，避免额外安装宿主环境：
docker run --rm --entrypoint python --user "$(id -u):$(id -g)" \
  -v "$PWD:/src" -w /src memulife-mcp:1.3.0 \
  ops/render_k8s_config.py --env-file .env --output .local/k3s-config.json
```

生成文件权限为 `0600`，包含 Kubernetes Secret。渲染器输出独立的 core、MCP、acquisition 配置，并将数据库凭据与模型密钥按角色分开。输入采用 [compose.env.example](compose.env.example) 的字段；它不是原生运行的 `.env.example`。

### 3. 应用配置

```bash
kubectl apply -f deploy/k8s/base/namespace.yaml
kubectl apply -f .local/k3s-config.json
kubectl apply -k deploy/k8s/base
kubectl -n memu wait --for=condition=Ready pod -l app=memu-postgres --timeout=600s
kubectl -n memu rollout status deployment/memu-core --timeout=900s
kubectl -n memu rollout status deployment/memu-mcp --timeout=600s
kubectl -n memu rollout status deployment/memu-acquisition --timeout=600s
```

core 的初始化容器使用数据库 Secret，在空数据库上建立仓库；主进程建立检索扩展与表。数据库启动较慢时，初始化容器可能由 Kubernetes 重新启动。已有卷不能靠重新运行初始化迁移为另一个仓库。

### 4. 本机访问

在两个终端分别执行：

```bash
kubectl -n memu port-forward service/memu-acquisition 5073:5073
kubectl -n memu port-forward service/memu-mcp 5071:5071
```

访问地址和登录 token 与 Compose 相同。生产环境可配置 TLS Ingress。示例 [ingress.example.yaml](k8s/ingress.example.yaml) 只代理 MCP；管理页使用独立域名或完整路径代理，核心与数据库不暴露公网。

外部数据库部署可在自己的 overlay 中移除 `postgres.yaml`，并替换两个应用 Secret 中的 `MEMU_DATABASE_URL`。数据库需要 pgvector、pg_trgm 和业务 schema 的读写权限。

## 主机名、浏览器来源与 OAuth

应用检查 Host 和浏览器 Origin。反向代理使用外部域名时，应分别更新：

- MCP：`MEMU_MCP_ALLOWED_HOSTS`、`MEMU_MCP_ALLOWED_ORIGINS`。
- 管理页面：`MEMU_ACQUISITION_ALLOWED_HOSTS`、`MEMU_ACQUISITION_ALLOWED_ORIGINS`。

值为 JSON 数组，精确列出主机和来源，不使用通配符。HTTPS 页面需配置 `https://域名` 的 Origin；OAuth 回调地址使用实际访问地址，且需在来源平台登记。更新配置后重新创建容器或更新 Pod，单纯重启不会重新注入旧容器的环境。

`/healthz` 只报告进程存活；`/readyz` 报告核心就绪或前端对核心的可达性。它们不包含凭据，也不调用模型。代理超时应覆盖核心请求超时，未知写入结果沿用原幂等键核对。

## 数据与迁移

必须配套保留三部分：数据库、记忆卷、采集卷。

| Compose 卷 | k3s 卷 | 内容 |
|---|---|---|
| `postgres-data` | PostgreSQL 的 `data` PVC | 权威控制、任务、修订历史和索引 |
| `memory-data` | `memu-data` | 正文、原件、媒体、仓库标记与文件发布状态 |
| `acquisition-data` | `memu-acquisition-data` | 来源凭据、OAuth 刷新状态、交付缓存 |

有数据迁移时，先停止采集和核心写入，再导出数据库与两个应用卷。恢复必须对应同一时点，保持仓库标记一致；仅复制 Markdown 会丢失事务与任务状态。新机器配置自己的模型地址和凭据，不使用旧机器的绝对路径。原理与恢复限制见[可靠写入](../docs/reliability.md)。

全新部署初始化数据卷后，在管理页面建立来源连接。

远端只部署 MCP 的可选路径见[远端 MCP 指南](remote-mcp/README.md)。

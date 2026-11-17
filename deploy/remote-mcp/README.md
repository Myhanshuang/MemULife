# 远端 MCP / 本机核心：Tailscale + Docker Compose

远端只运行 MCP 前端与 HTTPS 代理。记忆核心、存储、数据库、MLLM 和 embedding 留在核心主机。
两台机器用 Tailscale 通信；业务连接不需要 SSH 隧道。SSH 仅可作为服务器管理工具。

```text
客户端 → 公网 HTTPS /mcp → 远端 MCP
                              │ Tailscale 加密网络
                    核心主机 Tailscale IP:15072
                              │ 仅限 tailnet 的 TCP 转发
                    本机 127.0.0.1:5072 核心
                              └── 记忆 / 数据库 / MLLM / embedding
```

## 环境与私有配置

远端需要 Linux rootful Docker Engine、Compose 2.30+；两台主机均已加入同一个受信任 tailnet。
容器使用 host 网络，以使用宿主的 Tailscale 路由。模板不针对 rootless Docker 或 Docker Desktop。
tailnet 的访问规则必须允许远端访问核心主机 TCP 15072；应按实际调用方限制这条授权。

| 配置位置 | 内容 |
|---|---|
| 核心主机的私有 `.env` | 存储、数据库、模型、权限、身份 token、核心 Host 白名单 |
| 远端私有 `.env` | 公网域名、证书邮箱、镜像标签 |
| 远端私有 `mcp.env` | 核心 Tailscale 地址、需要的身份 token、前端设置 |
| 客户端私有配置 | 公网 URL、对应身份的 Bearer token |

不要把完整核心 `.env` 复制到远端。通常仅转交 `agent1` 的 token，不转交 owner、数据库连接串或模型密钥。
两端同一身份的 token 必须一致，核心白名单需授予该身份权限。实际配置文件权限设置为 `0600`。

## 1. 核心主机接入 Tailscale

先确认核心已运行，且监听 `127.0.0.1:5072`。查看该主机地址：

```bash
tailscale ip -4
tailscale serve status
```

在核心私有 `.env` 的 `MEMU_CORE_ALLOWED_HOSTS` 中加入上一步得到的**核心主机 Tailscale IP**，
保留原有 `127.0.0.1`、`localhost` 等主机名。只重启核心使 Host 白名单生效；已有请求会按服务停止策略排空。
存储路径、模型配置和前端身份 token 不需要修改。

TCP 15072 必须未被占用。以下两种转发方式选择一种，不要同时运行。

**用户级转发（无需更改 tailscaled 管理配置）**：安装 `socat`，从
`core-host/tailnet.env.example` 创建私有 `~/.config/memu/core-tailnet.env`，
填写核心和远端前端的 Tailscale IPv4 地址。将
`core-host/memu-core-tailnet.service.example` 复制为
`~/.config/systemd/user/memu-core-tailnet.service`，然后运行：

```bash
bash ops/services.sh restart core
systemctl --user daemon-reload
systemctl --user enable --now memu-core-tailnet.service
```

该转发只绑定核心 Tailscale IP，使用源 IP 限制只接受指定前端；核心保留 Bearer 鉴权。
用户服务需已启用 linger 才能在退出登录后和开机继续运行。IP 变动后更新私有配置并重启转发。
转发不自动重放业务请求，不启动或重启核心。

**Tailscale Serve（需要本机 Tailscale 管理权限）**：确认端口没有已有 Serve 配置，再运行：

```bash
bash ops/services.sh restart core
bash deploy/remote-mcp/core-host/tailscale-serve.sh start
```

脚本执行 `tailscale serve --bg --tcp=15072 tcp://127.0.0.1:5072`，仅对 tailnet 提供 TCP 转发，
由现有 tailscaled 保存并管理。需要本机 Tailscale 管理权限。**不要使用 Funnel**；核心不发布到公网。
TCP 转发保留 HTTP Host，因此 Host 白名单必须包含前端实际使用的 Tailscale IP。
如需更换端口，通过 `MEMU_TAILNET_PORT` 指定，并同步修改远端 URL。

HTTP 链路的跨机部分由 Tailscale 加密。配置校验仅检查 IP 属于 Tailscale 地址段，不能证明网络路由正确；
部署时必须确认该地址确属目标 tailnet 主机。普通局域网或公网 HTTP 地址会被验证器拒绝，可改用 HTTPS。

## 2. 导出和配置远端

在源码根目录执行 `python3 ops/package_release.py --output /指定目录/memulife.tar.gz`。
将生成的源码包与 `.sha256` 传到远端，校验后解压；不要传输整个开发工作目录。

```bash
sha256sum -c memulife.tar.gz.sha256
tar -xzf memulife.tar.gz
cd memulife-1.3.0/deploy/remote-mcp
umask 077
cp .env.example .env
cp mcp.env.example mcp.env
chmod 600 .env mcp.env
```

编辑 `.env` 的公网域名和证书邮箱；编辑 `mcp.env`：

- `MEMU_CORE_URL=http://核心主机TailscaleIP:15072`；示例中的 `100.64.0.10` 必须替换。
- `MEMU_MCP_TOKENS` 填核心已有身份/token 的 JSON 子集，`MEMU_MCP_IDENTITY` 选择其中一个身份。
- 浏览器调用时，`MEMU_MCP_ALLOWED_ORIGINS` 填完整 Origin；无浏览器访问保持 `[]`。

两个配置文件都不要 `source`。`mcp.env` 使用 Compose `format: raw`：JSON **不加外层单引号**，
美元符号不进行插值；与核心 dotenv 文件格式有区别。空 token、示例公网域名和误复制的模型/数据库配置会被拒绝。

```bash
bash deploy.sh build
bash deploy.sh validate
bash deploy.sh up
bash deploy.sh check-local
bash deploy.sh check
```

`check-local` 验证 MCP → Tailscale → 核心的鉴权与工具发现；`check` 使用公开 HTTPS URL 验证整条路径。
两者均不调用模型、不写记忆。还应从外部客户端验证公网可达性。
资源较小的远端可以在同架构构建机上构建镜像，再 `docker save` / `docker load`，避免远端编译依赖。

## 3. HTTPS 与已有公网入口

模板中的 Caddy 独占 TCP 80/443，要求域名 A/AAAA 正确指向远端、端口空闲且可达；不要保留失效的 AAAA。
长耗时模型请求需考虑 CDN 超时，直接 DNS 解析通常更合适。证书由 Caddy 自动签发和续期，保留其数据卷。

若远端 443 已被其他网关使用，用 `bash deploy.sh up-mcp` 只启动前端，然后接入现有入口：

- HTTP 反向代理：将 `/mcp` 和 `/v1/memory/write` 转到 `127.0.0.1:5071`，保留 Host、Origin、Authorization、Accept 和 MCP 协议头，
  请求等待时间至少 900 秒，关闭请求重试。
- TLS SNI 分流：为该域名单独分配回环 TLS 后端。例如另起 Caddy 监听 `127.0.0.1:15443`，
  使用 `https_port 15443`、`default_bind 127.0.0.1`、`auto_https disable_redirects`，公网 443 按域名原样转发 TLS。
  禁用 HTTP challenge 后可通过该 TLS 路由完成 TLS-ALPN challenge；需直接 DNS，不经终止 TLS 的 CDN。

不要覆盖既有入口配置；先备份、检查配置并验证原有站点，再执行平滑重载。

## 客户端、维护和回退

填写 `client.example.json` 的公网 URL 与该身份 token，客户端需支持 Streamable HTTP 和自定义 Bearer 头。
所有工具调用的 `from` 必须等于认证身份，工具超时至少 900 秒。服务没有 OAuth 凭据发放接口。

```bash
bash deploy.sh status
bash deploy.sh logs
bash deploy.sh up  # 修改环境变量后重新创建容器；restart 不刷新环境变量
bash deploy.sh down  # 不删除证书卷
# 核心主机按实际选用的方式停用转发：
systemctl --user disable --now memu-core-tailnet.service
# 或仅停用本服务的 Serve 端口，不影响其他 Serve 配置：
bash deploy/remote-mcp/core-host/tailscale-serve.sh stop
```

`/readyz` 只检查核心探针，不验证 token、数据库或模型；以鉴权的 `check-local` 为连接验收。
探针间隔 150 秒，unhealthy 本身不会触发 Docker 重启。网络中断后写入结果可能未知，核查或重试保持原幂等键。
停用远端时，保留本机核心、记忆和模型；前端回退只需换回先前镜像。

参考：[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)、
[Docker host 网络](https://docs.docker.com/engine/network/drivers/host/)、
[Caddy 端口设置](https://caddyserver.com/docs/caddyfile/options#https_port)。

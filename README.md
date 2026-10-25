# 私人凭据库 · NAS MCP Console

把 SSH 信息、账号密码、API 密钥和 Token 长期保存在自己的 NAS，在新对话和其他设备上通过 MCP 按授权获取，省去反复手动粘贴。

提供中文可视化后台。飞牛部署配置默认拉取 `docker.io/jinshenganyuci/nas-mcp-console:0.2.0`，无需在 NAS 上构建镜像。

**0.2.0 发布状态：待镜像上传与公开拉取验证完成。**

## 功能

- 可视化新增、编辑、删除凭据。秘密字段留空表示保留旧值。
- 支持 SSH root/其他账号＋密码、SSH 私钥及口令、Cloudflare API Token/Global API Key、Alist 账号密码、GitHub Token、Docker Hub/GHCR Token。
- 后台提供“测试凭据”，确认录入的认证信息能否使用。
- 每个客户端独立生成访问 Token，可授权全部凭据或指定条目，支持撤销。
- 凭据加密持久化，重启服务或容器后仍可使用。
- MCP只暴露 `list_credentials` 和 `get_credentials` 两个只读工具。

不包含 SSH 命令执行、Cloudflare 资源查询、GitHub 建仓或 Docker 镜像操作。

## 使用流程

1. 在后台录入一次服务别名、连接信息和秘密值，例如 SSH 条目 `home-nas`。
2. 给某台电脑或 AI 客户端创建访问 Token，选择允许读取哪些凭据。
3. 将 MCP 地址与客户端 Token 保存到支持的客户端。
4. 新对话里直接说明任务。AI先查找凭据，再获取所需字段，由当前客户端环境决定如何使用。

调用示例：

```json
{"name":"list_credentials","arguments":{"query":"home-nas"}}
```

```json
{"name":"get_credentials","arguments":{"credential_id":"ssh/home-nas","fields":["password"]}}
```

`list_credentials`只返回授权范围内的名称、连接信息和可取字段。
`get_credentials`按请求返回真实凭据；省略fields时返回该条目的全部秘密字段。
这些值会进入调用客户端的工具上下文，客户端之后的保存和使用由该客户端控制。
后台编辑页不回读已存秘密值，只显示是否已保存。

## 在飞牛部署

使用独立文件 [`compose.fnos.yaml`](compose.fnos.yaml)。在飞牛 Docker 的 Compose 管理中新建项目，粘贴该文件内容并启动。默认端口为 `8090`，无需下载源码、填写账号密钥或执行构建。

也可以把这个文件保存到 NAS 的项目目录，在该目录执行：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
```

默认使用名为 `nas-mcp-data` 的 Docker 命名卷，挂载到容器 `/data`。镜像预设该目录归 UID/GID `1000:1000` 所有；新建命名卷继承目录权限，容器以该用户运行，首次启动即可写入。默认部署不需要准备相对 `data` 目录或设置 PUID/PGID。

首次登录：

1. 打开 `http://NAS内网IP:8090/`。
2. 在飞牛 Docker 的卷管理中找到 `nas-mcp-data`，通过卷的挂载位置读取 `first-login.txt`。目录仅允许所有者访问，需使用有读取权限的 NAS 管理员操作。若卷管理不支持直接浏览文件，可在该容器的终端执行 `cat /data/first-login.txt`；命令行等价操作为 `docker compose -f compose.fnos.yaml exec nas-mcp cat /data/first-login.txt`。
3. 用临时密码登录并设置自己的管理密码。修改成功后，`first-login.txt` 会自动删除。
4. 在后台添加凭据，创建客户端授权并复制 MCP 地址与客户端 Token。

地址加上 `?demo=1` 可看虚构数据演示；演示不发送 API 请求，不保存真实凭据。正常使用请打开不带 demo 参数的地址。

可通过飞牛项目环境变量或 Compose 文件同目录的 `.env` 修改以下选项；无需修改时可不创建 `.env`：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MCP_IMAGE` | `docker.io/jinshenganyuci/nas-mcp-console:0.2.0` | 镜像地址及版本 |
| `MCP_PORT` | `8090` | NAS 对外端口 |
| `BIND_ADDRESS` | `0.0.0.0` | NAS 监听地址 |
| `MCP_DATA_VOLUME` | `nas-mcp-data` | 持久化卷名；已有数据后保持不变 |
| `ALLOWED_HOSTS` | 空 | 额外允许的域名，以逗号分隔；也可在后台设置 |

默认 Docker Hub 仓库 `jinshenganyuci/nas-mcp-console` 已设为公开；版本镜像的上传和拉取结果以本文顶部发布状态为准。备用地址为 `ghcr.io/jinshenganyuci/nas-mcp-console:0.2.0`，尚待公开访问验证，通过后可将 `MCP_IMAGE` 改为该地址使用。

## 录入与可用性测试

| 类型 | 录入内容 | 后台测试 |
| --- | --- | --- |
| SSH | 地址、端口、用户名（默认root）、密码或私钥、可选密钥口令 | 仅检查握手及登录，不运行命令；首次核对主机指纹后才发送登录凭据 |
| Cloudflare | API Token，或邮箱＋Global API Key | 验证Token或当前身份 |
| Alist | 地址、用户名、密码 | 仅尝试登录，不操作文件；暂不支持动态OTP |
| GitHub | 用户名、访问Token | 仅验证Token对应身份 |
| Docker Hub/GHCR | 登录用户名、命名空间、访问Token | 仅验证仓库认证，不连接Docker Engine或推送镜像 |

测试仅对后台管理员开放，不暴露为MCP工具。成功只代表该次认证可用，不保证拥有所有业务权限。
容器里的localhost指容器自身，NAS上其他服务应填写容器可达的内网地址。

## 跨对话与跨设备

MCP入口为 `https://你的域名/mcp`，使用Streamable HTTP。
支持Bearer Token的桌面客户端使用后台创建的客户端Token，不能用管理员密码代替。

```toml
[mcp_servers.credential_vault]
url = "https://你的域名/mcp"
bearer_token_env_var = "NAS_MCP_TOKEN"
```

环境变量必须对客户端进程可见；图形客户端不一定继承终端的export，也可用客户端提供的凭据设置或钥匙串助手。
每台新设备需添加一次MCP连接，之后在启用该连接的新对话中复用，不必分别登录每个平台。

ChatGPT网页端还需要账户支持的插件/MCP连接方式与兼容认证。
本项目可选外部OAuth资源服务器支持：配置发行者、JWKS、audience、允许的用户subject和required scopes，保存后重启。
登录、同意授权、PKCE和客户端注册由外部身份提供方负责；本项目不包含OAuth登录站点。
白名单中的OAuth用户视为库所有者，可读取全库；需要条目隔离时使用独立客户端Token。
尚未完成真实ChatGPT网页账号与身份提供方的联调。

## 内外网访问

公网访问需反向代理提供HTTPS，并在后台设置域名；内网基础地址可留空，自动使用当前地址。
正式使用建议采用HTTPS，避免明文传输凭据。代理需保留Host、不缓存API响应，并转发/mcp及OAuth所需/.well-known路径。
可以在代理上把管理后台限制到内网，仅向外开放MCP和所需发现路径。

## 保存与备份

容器的数据都保存在 `/data`，默认对应命名卷 `nas-mcp-data`。重启、更新镜像、重建或删除容器都不会自动删除这个卷；请勿删除数据卷或执行 `docker compose down -v`。改用另一个卷名会看到一个新库，原有数据仍在原卷中。

秘密值以Fernet加密保存在 `/data/vault.json`，主密钥在 `/data/master.key`，账号配置与客户端授权在 `/data/state.json`；取用或后台验证时才解密到内存。
客户端Token仅保存散列，管理员密码使用scrypt派生散列。

备份前停止此服务，再用飞牛的卷备份功能或从卷挂载位置复制**整个数据卷**，随后启动服务。恢复时先停服务，把同一份备份的全部文件恢复到原数据卷，保持 UID/GID `1000:1000`，再启动。不能只备份 `vault.json`：缺少配套的 `master.key` 就无法解密，缺少 `state.json` 也无法完整恢复条目与授权。

保护备份。持有主密钥和密文的人可以解密，文件加密不能代替NAS目录权限。镜像不包含密码、密钥或账号配置；`data`、`.env`、本地环境不应提交 Git，Docker 构建上下文也排除持久数据。

## 更新版本

先备份数据卷。将 `MCP_IMAGE` 改为希望升级到的已发布版本标签，保持 Compose 项目和 `MCP_DATA_VOLUME` 不变，在飞牛 Compose 管理中拉取镜像并重新创建容器；命令行等价操作是：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
docker compose -f compose.fnos.yaml ps
```

确认容器健康后访问后台，原凭据和客户端授权仍应存在。版本标签固定为 `0.2.0` 时，上述命令不会自动改成未来的新版本。需要回退时使用原版本镜像；若新版本改变了数据格式，应同时恢复升级前的完整备份。

## 可选：使用 NAS 相对目录

默认命名卷无需手动准备目录权限。若希望直接在项目目录看到数据，可自行把 `compose.fnos.yaml` 的挂载改为 `./data:/data`。相对路径相对于 Compose 文件所在目录；应先创建目录，并确保其所有者与容器的 UID/GID 一致，权限为 `700`。默认容器 UID/GID 是 `1000:1000`，新建 bind 目录不会自动继承镜像内的目录权限。

如果改用 NAS 上另一个账户的 UID/GID，还需同时修改 Compose 中的 `user` 与目录所有者。已有命名卷改为 bind 挂载时，应停服务并先复制整个原卷数据，否则看到的会是空库。该选项不需要额外开放容器权限。

## 源码开发与本地体验

需要 Python 3.12：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt
NAS_MCP_DATA=./data .venv/bin/uvicorn app.server:app_factory --factory --host 127.0.0.1 --port 8090 --no-access-log --no-proxy-headers
```

打开 `http://127.0.0.1:8090/`，首次密码位于源码目录的 `data/first-login.txt`；增加 `?demo=1` 查看演示。

源码附带的 `compose.yaml` 专用于本地构建，使用 `./data` bind 挂载。运行 `sh prepare.sh` 后，在 `.env` 中将 PUID/PGID 设为该目录所有者的 UID/GID，再执行 `docker compose -f compose.yaml up -d --build`。飞牛直接使用发布镜像时选择 `compose.fnos.yaml`，无需执行这个开发流程。

## 测试

```sh
.venv/bin/pip install pytest -c constraints.txt
.venv/bin/python -m pytest -q
```

测试使用临时或虚构凭据及 mock 网络，不连接真实账号。普通 push/PR 的 GitHub Actions 运行测试、JS 语法和 Compose 配置检查。版本 tag 或手动触发的发布流程先运行 186 项测试，再验证真实容器启动及重建后的数据持久化，通过后构建 `linux/amd64` 与 `linux/arm64` 双架构镜像，并上传 Docker Hub 和 GHCR。

参考：[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)、[桌面与网页MCP](https://learn.chatgpt.com/docs/extend/mcp)、[Cloudflare凭据](https://developers.cloudflare.com/fundamentals/api/get-started/)、[Docker访问Token](https://docs.docker.com/security/access-tokens/)。

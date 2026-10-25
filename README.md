# 私人凭据库 · NAS MCP Console

把 SSH 信息、账号密码、API 密钥和 Token 长期保存在自己的 NAS，在新对话和其他设备上通过 MCP 按授权获取，省去反复手动粘贴。

提供中文可视化后台。飞牛部署配置默认拉取 `docker.io/jinshenganyuci/nas-mcp-console:0.3.0`，无需在 NAS 上构建镜像。

**0.3.0 新增内置 OAuth 登录授权，可从支持 OAuth 的自定义 MCP 页面连接。保留中文凭据名称及已有客户端 Token。镜像提供 `linux/amd64`、`linux/arm64` 两种架构。**

- Docker Hub：`docker.io/jinshenganyuci/nas-mcp-console:0.3.0`
- GHCR：`ghcr.io/jinshenganyuci/nas-mcp-console:0.3.0`
- 两个仓库也提供 `latest` 标签；部署配置默认固定版本，方便控制升级。

## 功能

- 可视化新增、编辑、删除凭据。凭据名称支持中文，例如 `家里NAS`、`香港服务器`；秘密字段留空表示保留旧值。
- 支持 SSH root/其他账号＋密码、SSH 私钥及口令、Cloudflare API Token/Global API Key、Alist 账号密码、GitHub Token、Docker Hub/GHCR Token。
- 后台提供“测试凭据”，确认录入的认证信息能否使用。
- 支持内置 OAuth：在本库登录、选择凭据范围并同意，后台可查看并撤销授权。
- 也可为每个客户端独立生成访问 Token，授权全部凭据或指定条目。
- 凭据加密持久化，重启服务或容器后仍可使用。
- MCP只暴露 `list_credentials` 和 `get_credentials` 两个只读工具。

不包含 SSH 命令执行、Cloudflare 资源查询、GitHub 建仓或 Docker 镜像操作。

## 使用流程

1. 在后台录入一次凭据名称、连接信息和秘密值，例如 SSH 条目 `家里NAS`。
2. 在客户端添加 MCP 地址，使用 OAuth 登录并选择允许读取哪些凭据；也可在后台创建独立客户端 Token。
3. 保存客户端连接，后续对话复用此授权。
4. 新对话里直接说明任务。AI先查找凭据，再获取所需字段，由当前客户端环境决定如何使用。

“凭据名称”是你和 AI 查找这份账号或密钥时使用的名字，不是登录用户名；“备注”是可选的用途说明。名称为 1～64 个字，支持中文、英文、数字以及短横线、下划线，以文字或数字开头，不含空格。名称保存后固定，编辑时可修改备注及连接信息；已有英文名称不变，原客户端授权继续有效。

调用示例：

```json
{"name":"list_credentials","arguments":{"query":"家里NAS"}}
```

```json
{"name":"get_credentials","arguments":{"credential_id":"ssh/家里NAS","fields":["password"]}}
```

`list_credentials`只返回授权范围内的名称、连接信息和可取字段。
`get_credentials`按请求返回真实凭据；省略fields时返回该条目的全部秘密字段。
这些值会进入调用客户端的工具上下文，客户端之后的保存和使用由该客户端控制。
后台编辑页不回读已存秘密值，只显示是否已保存。

## 在飞牛部署

使用独立文件 [`compose.fnos.yaml`](compose.fnos.yaml)，各配置项附有中文注释。在飞牛 Docker 的 Compose 管理中新建项目，指定项目目录并粘贴文件内容。端口直接配置为 `8090:8090`，数据挂载为 `./data:/data`，无需下载源码、创建 `.env` 或执行构建。

`./data` 相对于 **Compose 文件所在目录**，对应容器内的 `/data`。启动前先创建该目录，目录所有者必须与 Compose 的 `user` 一致，默认是 UID/GID `1000:1000`。如果飞牛账户或已有目录的所有者不是这个 UID/GID，可用 `id` 查看账户信息，再把 `user` 改成该账户的数值；同时确保 `data` 目录归该账户所有、权限为 `700`。Docker 自动创建的 bind 目录可能归 root 所有，不能假设容器用户能写入。

使用默认 `1000:1000` 时，可在 Compose 文件所在目录一次性新建空目录；以下命令需要管理员权限，root 会话可省略 `sudo`。若 `data` 已存在，先检查其所有者，并让 `user` 匹配原数据的所有者：

```sh
mkdir -m 700 ./data && sudo chown 1000:1000 ./data
```

完成目录权限设置后，在飞牛中启动项目；也可以在该目录执行：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
```

首次登录：

1. 打开 `http://NAS内网IP:8090/`。
2. 在项目目录打开 `./data/first-login.txt` 读取临时密码。目录仅允许所有者访问，请使用对应账户或有读取权限的管理员操作；也可在容器终端执行 `cat /data/first-login.txt`。
3. 用临时密码登录并设置自己的管理密码。修改成功后，`first-login.txt` 会自动删除。
4. 在后台添加凭据，创建客户端授权并复制 MCP 地址与客户端 Token。

地址加上 `?demo=1` 可看虚构数据演示；演示不发送 API 请求，不保存真实凭据。正常使用请打开不带 demo 参数的地址。

需要调整时，直接编辑 `compose.fnos.yaml`，不使用 `.env` 插值：

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `image` | `docker.io/jinshenganyuci/nas-mcp-console:0.3.0` | 镜像地址及版本 |
| `ports` | `8090:8090` | 冒号左边是 NAS 端口；更换入口端口时只改左边 |
| `user` | `1000:1000` | 与 `./data` 所有者匹配的 UID/GID |
| `volumes` | `./data:/data` | 项目目录下的持久化数据；已有数据后保持挂载位置不变 |
| `NAS_MCP_ALLOWED_HOSTS` | 空字符串 | 额外允许的域名，以逗号分隔；也可在后台设置 |

Docker Hub 与 GHCR 镜像均为公开，无需配置镜像仓库登录即可拉取。若当前网络访问 Docker Hub 不便，可将 `image` 改为备用地址 `ghcr.io/jinshenganyuci/nas-mcp-console:0.3.0`。

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

### 通过 OAuth 页面直接添加

1. 在凭据库后台“设置访问入口”填写 **对外连接地址**，例如 `https://vault.example:8794`，不要带 `/mcp`。反向代理应已提供有效 HTTPS。未配置外部身份提供方时会自动使用内置 OAuth。
2. 在 ChatGPT / Codex 支持的“添加自定义 MCP 服务器”页面填写名称，服务器 URL 为 `https://vault.example:8794/mcp`，身份验证选择 **OAuth**。
3. 保留 OAuth 自动发现和动态客户端注册，无需手填 Client ID、Client Secret、Issuer 或 JWKS。
4. 在跳出的**私人凭据库页面**输入本库管理密码，核对客户端和回调域名，勾选允许读取的凭据，再点击“同意并继续”。也可明确选择“全部凭据”，这会包括今后新增的条目。
5. 回到客户端完成添加。后台“客户端授权 → 网页登录授权（OAuth）”可以查看范围、有效期和最近使用时间，也可立即撤销。

初始密码尚未修改时，先到管理后台完成首次设置。每次新授权都需要明确同意，不会因已登录后台而自动授权全库。管理密码只提交到本库；客户端拿到专属 OAuth 令牌，不会拿到管理密码。

授权有效期为 **30 天**；访问令牌为 15 分钟，支持刷新的客户端可在这 30 天内自动续期，无需每次聊天重新登录。超过授权有效期、管理员撤销或更改对外连接地址后需要重新连接。授权码只能使用一次，刷新令牌每次使用后轮换；重复使用旧刷新令牌会撤销该次授权。未完成的授权页面有效期为 10 分钟，服务重启后需重新发起；已完成的授权可以跨容器重建继续使用。

提供 OAuth 发现、动态客户端注册（DCR）、Authorization Code + PKCE S256、资源绑定、刷新与撤销端点。内置 OAuth 与手动客户端 Token 可同时使用。能否在具体账号上添加，还取决于该客户端开放的功能；自动化测试覆盖协议与本服务流程，真实账号连接需要部署后完成。

已有外部身份提供方的用户，可在高级设置中保留发行者、JWKS、audience、允许的 subject 和 required scopes，保存后重启。启用外部 OAuth 会取代内置登录流程；外部白名单身份仍视为库所有者，可读取全库。需要条目隔离时使用内置 OAuth 或独立客户端 Token。

## 内外网访问

公网访问需反向代理提供HTTPS，并在后台设置域名；内网基础地址可留空，自动使用当前地址。
正式使用采用 HTTPS。最简单的方式是将整个独立域名反向代理到本服务，保留 Host（含端口）、查询参数和请求正文，不缓存响应。应用在私有代理链路接收 HTTP，浏览器仍应访问公网 HTTPS 地址。

如需在代理上限制后台，请同时放行 OAuth 所需的 `/authorize`、`/token`、`/register`、`/revoke`、`/.well-known/*`、`/oauth/consent`、`/api/login`、`/api/oauth/request`、`/api/oauth/decision` 和授权页 `/static/*`，以及 `/mcp`。只放行 MCP 和发现路径无法完成网页登录授权。

## 保存与备份

容器的数据都保存在 `/data`，对应 Compose 项目目录下的 `./data`。重启、更新镜像、重建或删除容器都不会自动删除这个 NAS 目录。不要删除或清空 `data`；移动 Compose 文件或修改挂载位置时，应同时迁移原有完整数据，否则新位置会初始化为空库。

秘密值以Fernet加密保存在 `/data/vault.json`，主密钥在 `/data/master.key`，账号配置与客户端授权在 `/data/state.json`；取用或后台验证时才解密到内存。
客户端 Token 仅保存散列，管理员密码使用 scrypt 派生散列。内置 OAuth 状态持久化到 `/data/oauth.json`：授权码和访问/刷新令牌仅存散列，动态客户端密钥使用同一主密钥加密；文件权限为 600。该文件也必须包含在完整备份中。当前服务使用单进程，不支持多个实例同时写入同一数据目录。

备份前停止此服务，复制**整个 `data` 目录**，随后启动服务。恢复时先停服务，把同一份备份的全部文件恢复到挂载的 `data` 目录，确保目录和文件的所有者仍与 Compose 的 `user` 一致，再启动。不能只备份 `vault.json`：缺少配套的 `master.key` 就无法解密，缺少 `state.json` 也无法完整恢复条目与授权。

保护备份。持有主密钥和密文的人可以解密，文件加密不能代替NAS目录权限。镜像不包含密码、密钥或账号配置；`data`、`.env`、本地环境不应提交 Git，Docker 构建上下文也排除持久数据。

## 更新版本

先备份整个 `data` 目录。直接将 Compose 的 `image` 改为希望升级到的已发布版本标签，保持 Compose 文件位置、`./data:/data` 挂载与 `user` 不变，在飞牛 Compose 管理中拉取镜像并重新创建容器；命令行等价操作是：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
docker compose -f compose.fnos.yaml ps
```

确认容器健康后访问后台，原凭据和客户端授权仍应存在。版本标签固定为 `0.3.0` 时，上述命令不会自动改成未来的新版本。需要回退时使用原版本镜像；若新版本改变了数据格式，应同时恢复升级前的完整备份。

`0.2.0` 不支持中文名称；在 `0.2.1` 或更高版本创建中文名称后，如需回退到 `0.2.0`，请同时恢复升级前的完整备份。

## 从旧命名卷切换

如果已使用旧版的 `nas-mcp-data` 命名卷（或自行指定的卷名），新的 `./data` 挂载**不会自动迁移数据**。先停止原服务并备份整个旧卷，通过飞牛卷管理找到旧卷的挂载位置，把该卷全部文件复制到 Compose 文件旁的新 `data` 目录；不要把新旧库的文件混在一起。

将新目录及其文件的所有者设为 Compose `user` 指定的 UID/GID，确认目录权限为 `700`，再切换为 `./data:/data` 并启动。验证原凭据、管理员密码和客户端授权均正常后，再决定是否删除旧卷；迁移确认前保留原卷和备份。

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

测试使用临时或虚构凭据及 mock 网络，不连接真实账号。普通 push/PR 的 GitHub Actions 运行测试、JS 语法和 Compose 配置检查。版本 tag 或手动触发的发布流程先运行完整测试，再验证真实容器启动、OAuth 登录授权及重建后的凭据与刷新令牌持久化，通过后构建 `linux/amd64` 与 `linux/arm64` 双架构镜像，并上传 Docker Hub 和 GHCR。

参考：[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)、[桌面与网页MCP](https://learn.chatgpt.com/docs/extend/mcp)、[OpenAI 插件 OAuth](https://developers.openai.com/plugins/build/auth)、[Cloudflare凭据](https://developers.cloudflare.com/fundamentals/api/get-started/)、[Docker访问Token](https://docs.docker.com/security/access-tokens/)。

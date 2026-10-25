# 私人 NAS MCP

在飞牛 NAS 上用 Docker Compose 运行的个人 MCP 服务，带中文可视化管理后台。
后台管理 Cloudflare、SSH 和 Alist 的连接凭据；AI 客户端通过 `/mcp` 使用已配置的服务。

当前为本地开发验收版。代码已提供 Dockerfile 与 Compose 配置，但尚未构建或发布 Docker 镜像，尚未在真实 NAS 或真实服务上联调。

## 本地体验

需要 Python 3.12。先安装依赖，再仅监听本机地址：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt
NAS_MCP_DATA=./data .venv/bin/uvicorn app.server:app_factory --factory --host 127.0.0.1 --port 8090 --no-access-log --no-proxy-headers
```

打开 `http://127.0.0.1:8090/?demo=1` 查看虚构数据演示；打开不带 `?demo=1` 的地址可实际保存和管理连接。
首次真实登录密码在 `data/first-login.txt`，登录后须修改。
演示中“添加 SSH”直接显示 root 与密码输入框，“添加 Alist”直接显示用户名与密码；演示凭据框禁用，实际管理模式可输入。

本地验证包含88项后端测试及界面DOM与真实本地API的交互检查。没有用真实凭据测试远程服务；真实浏览器自动截图因本机进程权限限制未完成。

## 目前提供的功能

- 浏览器管理后台：新增、编辑、删除服务，测试连接；已存凭据只显示配置状态，可以替换，不能回读。
- Cloudflare：列出域名、查看 DNS 记录，支持分页及记录名称筛选。
- SSH：密码或私钥登录、主机密钥校验，执行管理员预配置的命名检查。
- Alist：登录、列目录、登录失效后重新认证，限制可访问的根目录。
- 每台 MCP 客户端单独生成访问 Token，可随时撤销。凭证只在创建时显示一次。
- 可选外部 OAuth 身份提供方接入：JWT 验证、用户白名单及 MCP 资源发现。

此版本不包含 Cloudflare 修改 DNS、Alist 上传删除或通用任意 SSH 命令工具。SSH 检查会执行你保存的命令，命令是否修改系统取决于你的配置。

## 后续在飞牛部署

1. 将本目录上传到飞牛存储中的应用目录，例如你的 `docker/nas-mcp` 文件夹。
2. 在此目录执行 `sh prepare.sh`，或用文件管理器建立 `data` 文件夹并复制 `.env.example` 为 `.env`。
3. 用 `id` 查看该目录所属账户的 UID/GID，在 `.env` 填写 `PUID` 和 `PGID`，让容器能写入 `data`。
4. 在飞牛 Docker Compose 项目中选中 `compose.yaml`，构建并启动；也可以运行：

```sh
docker compose up -d --build
docker compose ps
```

默认映射 NAS 的 8090 端口。通过浏览器访问 `http://你的NAS内网IP:8090`。
启动后，在飞牛文件管理器中打开 `data/first-login.txt`，用其中的临时密码登录。
后台会要求先设置你自己的管理员密码（至少12个字符），成功后删除临时密码文件。

如果创建 data 目录的账户与 `PUID/PGID` 不一致，需要由 NAS 管理员调整该目录的所有者。
不要把目录权限设置为所有人可读写。遇到启动失败先查看 `docker compose logs --tail=50`。

## 用界面添加服务

### Cloudflare

打开“新增服务”→ Cloudflare，填写别名和 API Token。
查询功能需要目标域名的 Zone Read 与 DNS Read 权限。Token 对应账户/域名资源范围由 Cloudflare 控制。
此自建版本直接连接 Cloudflare API，不要求再串接 Cloudflare 官方 MCP。

### SSH

填写别名、IP或可解析主机名、端口、用户名，并选择密码或私钥认证。
密钥有口令时，同时填写密钥口令。必须提供已核对的 `known_hosts` 内容。
可以从你已经信任的 SSH 客户端复制对应主机记录，或获取主机公钥并与 NAS/服务器控制台显示的指纹核对。
不能仅因网络扫描返回了密钥就把它视为已验证。

添加检查名称和命令，例如 `containers` 对应 `docker ps`。不同 NAS 账户可能没有 Docker 权限；按目标主机的实际授权选择命令。
“测试连接”会执行保存列表里的第一条检查命令。建议将短时、无副作用的检查放在第一条。
运行命令的权限就是所配置 SSH 账户的权限。不要把输出环境变量或凭据文件的命令配置为检查。
超时会关闭SSH连接，但不能保证远端进程因此结束，检查命令本身也应有运行时限。

### Alist

填写服务地址、用户名、密码与根目录。容器内的 `localhost` 指 MCP 容器本身；访问 NAS 其他服务应填写容器可达的内网地址，或同一 Docker 网络中的服务名。
当前版本支持普通账号密码登录，不支持每次需要输入动态 OTP 的账号，也不支持额外的目录密码。
`path` 是 Alist 站点绝对路径；如果根目录设置为 `/资料`，调用路径也应从 `/资料` 开始。

保存后点“测试连接”，确认查询能成功。更换密钥时重新填写；编辑表单里的空凭据字段表示保留原值。

## 连接桌面客户端

在“客户端接入”里为每台电脑创建一个凭证。复制首次显示的 Token，添加 Streamable HTTP MCP 服务，地址为：

```text
https://你的MCP域名/mcp
```

客户端通过 `Authorization: Bearer <该设备Token>` 访问，不能用后台管理员密码代替。
支持直接配置远程 MCP 的 Codex 本地客户端可以采用下列用户级配置；环境变量需对实际运行客户端的进程可见，图形客户端不一定继承终端的 export：

```toml
[mcp_servers.private_nas]
url = "https://你的MCP域名/mcp"
bearer_token_env_var = "PRIVATE_NAS_MCP_TOKEN"
```

也可以使用客户端支持的凭据输入或本机钥匙串辅助命令。不要把真实 Token 放进共享项目或 Git。
在每台机器添加一次连接后，新对话可以复用已启用的连接。先问“列出我 NAS MCP 中的服务”，再用返回的别名提问。
不同客户端仍有自己的工具开关、账户权限和操作确认规则。

## 内网与公网访问

优先用同一个 HTTPS 域名在内外网访问。可以用现有反向代理，将这个域名转发到 NAS 的8090端口。
在“访问设置”中保存公网基础地址和允许访问的域名。初次配置域名前仍可通过内网 IP 登录。
反向代理应保留正确 Host，支持 `/mcp` 的 POST 及 OAuth 元数据路径，不缓存API响应，设置足够的请求超时。
不要直接把明文HTTP管理端口暴露到互联网；TLS由你的反向代理终结。内网HTTP也会明文传输登录信息，正式使用建议配置HTTPS。
如果希望管理后台只在内网使用，可在反向代理上限制 `/`、`/admin`、`/static/*`、`/api/*`，只向公网开放 `/mcp` 与 `/.well-known/*`。

## 接入 ChatGPT 网页的条件

当前交付版包含可配置的 OAuth **资源服务器**，不包含登录站点/身份提供方。
仅在界面里勾选 OAuth 不会自动产生网页登录能力；仍需准备实际兼容的外部 OAuth 服务。

在“访问设置”中配置：发行者URL、JWKS URL、令牌 audience、允许的用户 subject、所需 scope，以及公网 MCP 资源地址。
该 OAuth 服务须负责用户登录、同意授权、授权码+PKCE及客户端注册/预注册，并提供客户端可发现的授权元数据。
JWT须使用 RS256 或 ES256，包含有效的 exp、iss、aud、sub；用户sub必须在你的白名单中。
保存 OAuth 设置后重启容器，服务器会提供 MCP Protected Resource Metadata。
桌面设备 Token 在启用 OAuth 后仍可用；它们与 OAuth 客户端的令牌分别验证。

ChatGPT 网页端还需通过账户支持的插件/MCP连接方式安装并授权。账号可用功能与外部身份提供方需要实际联调；此项目尚未完成真实 ChatGPT 网页登录验证。

## 凭据与备份

服务凭据加密存入 `data/vault.json`，主密钥在 `data/master.key`。
MCP设备Token只保存散列；管理员密码以scrypt派生散列保存。
容器使用时将所需服务凭据解密到 `/tmp` 的私有目录，此目录为 tmpfs，重建后自动恢复。
主密钥与密文在同一个持久目录中：能读取完整 data 目录的人也能解密凭据；加密不能代替 NAS 文件权限和磁盘保护。
备份完整 data 目录并保护备份，不要只备份 vault.json，也不要把 data 放进公开同步目录。
管理页面保存凭据不会经过 AI；但 MCP 返回的 DNS、文件名或命令输出会被调用它的 AI 客户端看到。

## 验证与开发

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt pytest
.venv/bin/python -m pytest -q
```

测试使用临时凭据和mock外部服务，不需要真实账号。`/healthz`只说明进程存活，各真实服务需在后台分别测试。
管理界面的 `?demo=1` 是不连接后端的演示模式，用虚构数据展示界面，不能存储真实凭据。

技术参考：[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)、[桌面与网页 MCP 接入](https://learn.chatgpt.com/docs/extend/mcp)、[Cloudflare API Token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)、[Alist API](https://alistgo.com/guide/api/)、[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)。

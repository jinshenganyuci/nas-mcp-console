# 私人凭据库 · NAS MCP Console

把 SSH 信息、账号密码、API 密钥和 Token 长期保存在自己的 NAS，在新对话和其他设备上通过 MCP 按授权获取，省去反复手动粘贴。

提供中文可视化后台。当前为本地验收版，尚未构建或发布 Docker 镜像。

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

## 本地体验

需要 Python 3.12：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt
NAS_MCP_DATA=./data .venv/bin/uvicorn app.server:app_factory --factory --host 127.0.0.1 --port 8090 --no-access-log --no-proxy-headers
```

打开 `http://127.0.0.1:8090/?demo=1` 查看虚构数据演示。演示不发送API请求，不保存真实凭据。
打开不带demo参数的地址可实际使用后台；首次密码在 `data/first-login.txt`，登录后必须修改。

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

## 后续在飞牛运行

已提供Dockerfile和Compose配置，镜像构建留待本地验收后进行。
将项目放入飞牛应用目录，执行 `sh prepare.sh`，在.env填写目录所属账户的PUID/PGID；data目录所有者须与容器用户一致。

```sh
docker compose up -d --build
```

默认8090端口。data目录挂载到NAS持久存储；镜像不包含密码、密钥或账号配置。
公网访问需反向代理提供HTTPS，并在后台设置域名；内网基础地址可留空，自动使用当前地址。
正式使用建议采用HTTPS，避免明文传输凭据。代理需保留Host、不缓存API响应，并转发/mcp及OAuth所需/.well-known路径。
可以在代理上把管理后台限制到内网，仅向外开放MCP和所需发现路径。

## 保存与备份

秘密值以Fernet加密保存在 `data/vault.json`，主密钥在 `data/master.key`；取用或后台验证时才解密到内存。
客户端Token仅保存散列，管理员密码使用scrypt派生散列。
备份整个data目录并保护备份。持有主密钥和密文的人可以解密，文件加密不能代替NAS目录权限。
data、.env、本地环境不应提交Git，Docker构建上下文也排除持久数据。

## 测试

```sh
.venv/bin/pip install pytest -c constraints.txt
.venv/bin/python -m pytest -q
```

测试使用临时或虚构凭据及mock网络，不连接真实账号。GitHub Actions仅运行测试、JS语法和Compose配置检查，不构建发布镜像。

参考：[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)、[桌面与网页MCP](https://learn.chatgpt.com/docs/extend/mcp)、[Cloudflare凭据](https://developers.cloudflare.com/fundamentals/api/get-started/)、[Docker访问Token](https://docs.docker.com/security/access-tokens/)。

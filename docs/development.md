# 开发与维护

[返回首页](../README.md) · [部署指南](deployment.md) · [客户端接入](clients.md)

本页面向修改源码、运行测试和维护发布流程的用户。只想在 NAS 使用应用，直接按[发布镜像部署指南](deployment.md)操作即可。

## 从源码运行

使用 Python **3.12**，与 Dockerfile 和 CI 一致。前端是原生 HTML、CSS、JavaScript，不需要 npm 构建。

```sh
git clone https://github.com/jinshenganyuci/nas-mcp-console.git
cd nas-mcp-console
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c constraints.txt
NAS_MCP_DATA=./data .venv/bin/uvicorn app.server:app_factory --factory --host 127.0.0.1 --port 8090 --no-access-log --no-proxy-headers
```

打开 `http://127.0.0.1:8090/`。临时管理密码位于当前项目的 `data/first-login.txt`，首次登录后修改密码。加上 `?demo=1` 可查看前端的虚构演示数据；演示不会调用 API 或保存真实凭据。

`--no-access-log` 避免将带查询参数的授权请求写入 Uvicorn 访问日志；本应用自行处理 Host 与访问入口，不依赖未受约束的代理头。开发环境使用单进程，不添加多个 worker，也不让多个实例同时写同一个数据目录。

## 本地 Docker 构建

源码中的 [`compose.yaml`](../compose.yaml) 用于开发构建，发布镜像部署使用 [`compose.fnos.yaml`](../compose.fnos.yaml)。

```sh
sh prepare.sh
id
```

`prepare.sh` 创建 `data` 目录、设置目录权限，并在 `.env` 不存在时复制 `.env.example`。它不会自动修正已有文件的所有者；先在 `.env` 中把 `PUID`、`PGID` 设为数据目录实际所有者的 UID/GID。

```sh
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps
```

开发构建使用本地镜像标签 `private-nas-mcp:dev`。`.env` 可调整宿主机监听地址、端口和额外允许的 Host；它不需要包含 GitHub、Cloudflare 等上游账号秘密，这些通过应用后台录入。

## 代码结构

| 路径 | 用途 |
| --- | --- |
| `app/server.py` | 管理 API、认证与访问入口校验、两个 MCP 工具 |
| `app/store.py` | 凭据加密存储、条目管理、客户端 Token 与授权范围 |
| `app/validation.py` | SSH、Cloudflare、Alist、GitHub、镜像仓库认证测试 |
| `app/oauth_server.py` | OAuth 发现、注册、授权、Token 与撤销协议 |
| `app/oauth_store.py` | OAuth 持久化、授权码与刷新轮换、到期与撤销 |
| `app/static/` | 中文管理界面与 OAuth 同意页面 |
| `tests/` | 单元、API、协议测试和真实容器检查脚本 |
| `.github/workflows/ci.yml` | 测试、JavaScript 语法和 Compose 校验 |
| `.github/workflows/publish.yml` | 验证后发布双架构 Docker Hub / GHCR 镜像 |

## 执行检查

```sh
.venv/bin/python -m pip install pytest -c constraints.txt
.venv/bin/python -m pytest -q
node --check app/static/admin.js
node --check app/static/oauth.js
docker compose -f compose.yaml config --quiet
docker compose -f compose.fnos.yaml config --quiet
```

CI 使用 Node.js 22 做语法检查。普通测试使用临时目录、虚构凭据和 mock 网络，不需要提供真实账号。

需要验证真实容器行为时，使用 Docker 构建独立测试镜像，再运行现有脚本。脚本要求有 Docker 权限的非 root 用户、可用的 `python3`，以及空闲的本机 `127.0.0.1:18093` 端口；不要直接在 NAS 的 root 会话运行：

```sh
docker build -t nas-mcp-console:smoke .
sh tests/container-smoke.sh nas-mcp-console:smoke
```

脚本检查首次启动、管理登录、凭据持久化，以及 OAuth 授权和重建后的刷新流程，使用独立的临时测试数据。不要把生产 `data` 目录挂给开发或测试容器。

## 发布镜像

仓库工作流在推送 `v*` 标签或手动运行 `Publish container images` 时发布。手动输入和版本标签都必须是稳定语义版本，例如 `0.4.0`，不接受随意的标签字符串。

维护者需要在 GitHub 仓库的 Actions 设置中配置：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Repository variable | `DOCKERHUB_USERNAME` | Docker Hub 用户名与目标镜像命名空间 |
| Repository secret | `DOCKERHUB_TOKEN` | 有目标镜像仓库写权限的 Docker Hub PAT |
| GitHub 自动提供 | `GITHUB_TOKEN` | 工作流对当前仓库对应 GHCR 包的认证；发布任务声明 `packages: write` |

发布前先核对应用版本、发布 Compose 的固定版本、README 版本与工作流默认版本。不要把账号 Token 写进 YAML 或 Git 历史。

发布流程依次执行：

1. 运行 CI 中的全部检查。
2. 构建本机架构测试镜像并执行真实容器检查。
3. 构建 `linux/amd64` 与 `linux/arm64` 镜像。
4. 同时推送版本标签和 `latest` 到 Docker Hub 与 GHCR。
5. 在 Actions 摘要中记录镜像摘要。

普通 `main` 分支提交运行测试，**不会自动更新 `latest` 镜像**。文档修改可以直接发布到仓库；应用发布完成后再让部署端拉取镜像更新。

Fork 后需要使用自己的镜像仓库，并相应修改 Compose、README 中的镜像地址；首次创建的 GHCR 包可见性由 GitHub 包设置决定，面向公众部署时应确认支持匿名拉取。

## 修改与贡献约定

- MCP 保持“查询和获取获准凭据”的职责；上游资源操作由客户端执行。
- 修改持久化结构时同时考虑旧数据兼容、备份和恢复，不能静默重建损坏的库。
- 密码、Token、授权码和刷新令牌不出现在日志、错误消息、示例或测试快照中；测试使用虚构值。
- 改动配置字段或界面步骤时，同步更新对应文档。
- 提交前检查 `git diff --check` 与 `git status --short`，确认未包含真实 `data`、`.env`、备份或个人配置。

依赖版本分别记录在 `requirements.txt` 和 `constraints.txt`；升级依赖后执行对应测试及容器检查，再更新发布说明。

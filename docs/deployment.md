# 部署、HTTPS 与维护

[返回首页](../README.md) · [录入凭据](credentials.md) · [接入客户端](clients.md)

本页以飞牛 NAS 的 Docker Compose 为例，其他支持 Docker Compose 的 Linux 主机也可使用。服务提供管理网页和 MCP 接口，容器内固定监听 `8090`；所有持久化数据放在 Compose 文件旁的 `./data`。

## 1. 选择镜像和项目目录

下载仓库中的 [`compose.fnos.yaml`](../compose.fnos.yaml)。这是使用已发布镜像的部署配置，每项都有中文注释；不需要下载源码、运行构建或创建 `.env`。根目录的 [`compose.yaml`](../compose.yaml) 用于源码构建，不是本文的部署入口。

镜像提供 `linux/amd64`、`linux/arm64` 两种架构，Docker 会选择适合主机的架构。两个公开仓库都可匿名拉取：

| 镜像 | 适用方式 |
| --- | --- |
| `docker.io/jinshenganyuci/nas-mcp-console:0.3.0` | 随附部署文件的固定版本，升级时手动改版本号 |
| `docker.io/jinshenganyuci/nas-mcp-console:latest` | 希望每次主动拉取时升级到最新发布版 |
| `ghcr.io/jinshenganyuci/nas-mcp-console:0.3.0` | GHCR 备用镜像，固定版本 |
| `ghcr.io/jinshenganyuci/nas-mcp-console:latest` | GHCR 备用镜像，跟随最新发布版 |

`latest` 不会自动更新已经运行的容器，仍需拉取镜像并重新创建。版本与变更说明见 [GitHub Releases](https://github.com/jinshenganyuci/nas-mcp-console/releases)。

在飞牛中新建一个独立项目目录，例如：

```text
nas-mcp-console/
├── compose.fnos.yaml
└── data/
```

在飞牛 Docker 的 Compose 管理中创建项目，把项目目录指向这里，并使用 `compose.fnos.yaml` 的内容。不同版本的飞牛界面名称可能略有不同；关键是确认 Compose 的工作目录，避免把 `./data` 挂载到其他位置。

## 2. 准备数据目录权限

随附配置使用 `user: "1000:1000"`，含义是容器以 UID 1000、GID 1000 运行。`./data` 必须属于同一 UID/GID，应用才能读写、设置目录权限并保存凭据。

在项目目录执行以下命令，创建一个**新的空数据目录**：

```sh
mkdir -m 700 ./data && sudo chown 1000:1000 ./data
```

以 root 执行时省略 `sudo`。如果 `data` 已存在，先用下面的命令查看，不要清空它：

```sh
id
ls -ldn ./data
```

可将 Compose 的 `user` 改为现有数据目录所有者的数字 UID/GID；首次部署时，也可以由管理员将空目录交给计划使用的账户。目录权限应为 `700`。已有凭据时，修改运行用户还需要同时匹配目录内文件的所有者。

Docker 自动创建的 bind 挂载目录可能归 root 所有，不能假设默认容器用户可以写入它。目录准备好后再启动项目。

## 3. 端口和启动

默认配置很简单：

```yaml
ports:
  - "8090:8090" # NAS 端口:容器端口
volumes:
  - ./data:/data # 当前 Compose 项目目录下的 data
```

如果 NAS 的 `8090` 已占用，只修改左侧，例如 `"18090:8090"`，然后用 `http://NAS内网IP:18090/` 访问。右侧容器端口保持 `8090`。

在飞牛 Compose 管理中拉取镜像并启动项目；命令行等价操作为：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
docker compose -f compose.fnos.yaml ps
```

初次启动时健康状态可能先显示 `starting`；健康检查默认每 30 秒执行一次。如果无法启动，先检查日志：

```sh
docker compose -f compose.fnos.yaml logs --tail=100 nas-mcp
```

日志中若出现数据目录权限错误，检查上一节的 `user` 与所有者是否一致。若出现 `port is already allocated`，换一个左侧 NAS 端口。

## 4. 首次登录

1. 打开 `http://192.168.1.20:8090/`，将示例 IP 换成自己的 NAS 地址。
2. 在项目的 `./data/first-login.txt` 中读取临时管理密码。该目录只允许所有者访问，可使用对应账户或有权限的管理员打开。
3. 用临时密码登录，并设置自己的管理密码，至少 12 个字符。
4. 密码修改成功后，`first-login.txt` 自动删除。随后即可录入凭据和创建客户端授权。

也可在项目目录中通过容器读取一次临时密码：

```sh
docker compose -f compose.fnos.yaml exec nas-mcp cat /data/first-login.txt
```

已经完成首次设置后，这个文件不存在是正常现象；重启容器不会重新生成初始密码。

在地址后加 `?demo=1` 可查看虚构数据演示。演示模式不读取、保存真实凭据，正式使用应去掉该参数。

继续操作见 [凭据录入与可用性测试](credentials.md) 和 [客户端接入](clients.md)。

## 5. Compose 每项配置的含义

完整配置以 [`compose.fnos.yaml`](../compose.fnos.yaml) 为准。通常只需调整 `image`、`user` 和端口左侧。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `services` / `nas-mcp` | 一个服务 | 定义应用；命令中的 `nas-mcp` 就是服务名 |
| `image` | Docker Hub 的 `0.3.0` | 选择镜像仓库与版本，可改为 `latest` 或 GHCR 地址 |
| `restart` | `unless-stopped` | Docker 或 NAS 重启后恢复运行，手动停止的服务保持停止 |
| `init` | `true` | 使用轻量初始化进程，转发退出信号、回收子进程 |
| `user` | `1000:1000` | 容器运行的 UID/GID，应与数据目录所有者一致 |
| `ports` | `8090:8090` | NAS 入口端口映射到容器内服务端口 |
| `environment.NAS_MCP_DATA` | `/data` | 应用在容器内保存数据的位置 |
| `environment.NAS_MCP_ALLOWED_HOSTS` | 空字符串 | 额外允许访问的服务器域名或 IP，多个用逗号分隔，不带空格；也可在后台设置 |
| `volumes` | `./data:/data` | 把项目目录旁的数据文件夹挂进容器，实现持久化 |
| `read_only` | `true` | 容器应用文件系统只读；单独挂载的 `/data` 和 `/tmp` 仍可写 |
| `tmpfs` | `/tmp:size=32m,mode=1777` | 临时文件放在内存中，最多 32 MB，容器重建后不保留 |
| `cap_drop` | `ALL` | 去除额外的 Linux capabilities |
| `security_opt` | `no-new-privileges:true` | 阻止通过执行其他程序获得更高权限 |
| `healthcheck.test` | 请求容器内 `/healthz` | 检查服务能否响应，不读取或验证外部账号 |
| `healthcheck.interval` | `30s` | 每 30 秒检查一次 |
| `healthcheck.timeout` | `5s` | 单次健康检查最多等待 5 秒 |
| `healthcheck.retries` | `3` | 连续失败 3 次标记为不健康；这项本身不会自动重启容器 |
| `pids_limit` | `128` | 限制容器进程与线程总数 |
| `mem_limit` | `512m` | 限制容器内存 |
| `logging.driver` | `json-file` | 使用 Docker 文件日志，飞牛可查看 |
| `logging.options.max-size` | `5m` | 单个日志文件最多 5 MB |
| `logging.options.max-file` | `"2"` | 最多保留 2 个轮转日志文件 |

这里的 `./data` 相对于 Compose 项目目录。移动 Compose 文件、改变项目目录或修改挂载左侧，可能会让容器看到一个新空目录；迁移项目时必须同时迁移原数据。

<a id="access"></a>

## 6. 内网、公网与 HTTPS

### 只在内网使用

内网客户端能够访问 `http://NAS内网IP:8090/mcp` 时，可使用客户端 Token 接入。后台“对外连接地址”可留空；页面会基于当前访问地址显示 MCP 地址。

`localhost`、回环 IP 和私有 IP 已由服务允许，不需要把每台电脑或手机的 IP 填入访问域名列表。若使用自定义内网域名，则把该域名添加到后台“额外访问域名”。

### 用 Lucky 提供公网 HTTPS

以下为部署示例，不会自动修改 DNS、路由器或 Lucky：

```text
客户端 → https://vault.example.com:8443
                 ↓ Lucky 终止 TLS，反向代理整个站点
          http://192.168.1.20:8090
```

在 Lucky 的“Web 服务”中添加规则，启用 TLS 并选择覆盖 `vault.example.com` 的证书；添加反向代理子规则，前端域名填 `vault.example.com`，后端地址填 `http://192.168.1.20:8090`。监听端口可用可达的 `443` 或 `8443` 等空闲端口。具体页面说明见 [Lucky 官方 Web 服务文档](https://lucky666.cn/docs/modules/web/)。

还需满足以下连接条件：

- 域名解析到可访问的公网地址。若 Lucky 位于路由器后，公网入口端口应转发到 Lucky 实际监听的端口，并允许防火墙通过。
- Lucky 能访问后端地址。Lucky 在另一个容器中时，后端 `127.0.0.1` 通常指 Lucky 容器自身，应使用它可达的 NAS 地址或容器网络地址。
- 反向代理保留原始 `Host`，包括非默认端口，并转发 `Authorization`、查询参数和请求正文；不要缓存凭据和 OAuth 响应。
- 用独立域名代理整个站点，保留原路径。不要挂到 `/vault` 等子路径；应用对外连接地址不支持子路径前缀。
- MCP 客户端必须能从它的实际执行环境访问入口。浏览器能打开网页，不等于云端客户端或另一台机器也能连接。

公网 HTTPS 在 Lucky 处理后，Lucky 到容器可以走可信内网中的 HTTP。不要把外部 `Host` 改成后端 IP；OAuth 会核对域名和端口。

### 在后台保存入口地址

先通过 NAS 内网 IP 登录后台，进入“设置访问入口”：

| 后台字段 | 示例 | 说明 |
| --- | --- | --- |
| 对外连接地址 | `https://vault.example.com:8443` | 完整 HTTPS 基础地址；带实际端口，不带 `/mcp`、查询参数或其他路径 |
| 额外访问域名（旧界面可能显示“允许的 Host”） | `vault-alt.example.com` | 可选；每行一个额外的服务器域名或 IP，不带协议、路径、端口或通配符 |

**“允许的 Host”检查的是你访问凭据库时使用的服务器域名，不是访问者的 IP 白名单。** 填写对外连接地址后，该域名自动加入允许范围，无需再重复填写，也不用逐台添加电脑、手机或 AI 服务的 IP。

保存后按页面提示重启服务：

```sh
docker compose -f compose.fnos.yaml restart nas-mcp
```

对应的管理页面是 `https://vault.example.com:8443/`，MCP 地址是 `https://vault.example.com:8443/mcp`。使用内置 OAuth 时，对外连接地址是必需配置；详见 [OAuth 客户端接入](clients.md)。

若代理额外限制了后台路径，OAuth 流程仍需要 `/authorize`、`/token`、`/register`、`/revoke`、`/.well-known/*`、`/oauth/consent`、`/api/login`、`/api/oauth/request`、`/api/oauth/decision`、授权页使用的 `/static/*`，以及 `/mcp`。只转发 `/mcp` 无法完成本库网页登录和授权。

### 快速检查入口

```sh
curl -fsS http://192.168.1.20:8090/healthz
curl -fsS https://vault.example.com:8443/healthz
```

将示例替换为自己的地址。`/healthz` 成功代表服务可达，不代表客户端 Token 已获授权；后者应通过客户端自己的 MCP 连接测试验证。

直接在浏览器打开 `/mcp` 不是完整的 MCP 测试：没有授权时返回 `401` 属于预期；本服务使用无状态 Streamable HTTP，协议请求通过 `POST` 完成，不提供旧式 SSE 地址。

<a id="maintenance"></a>

## 7. 更新与回退

先备份整个 `data` 目录，再更新。保持项目目录、数据挂载和运行用户不变：

1. 使用固定版本时，将 `image` 改成 [Releases](https://github.com/jinshenganyuci/nas-mcp-console/releases) 中准备升级的版本；使用 `latest` 时无需改标签。
2. 在飞牛 Compose 管理中拉取镜像，再重新创建容器；仅重启容器不会换成新镜像。
3. 检查容器健康状态、后台版本、原有凭据及客户端连接。

命令行等价操作：

```sh
docker compose -f compose.fnos.yaml pull
docker compose -f compose.fnos.yaml up -d
docker compose -f compose.fnos.yaml ps
```

固定为 `0.3.0` 时，`pull` 不会把配置自动改成未来的新版本。使用 `latest` 时，每次拉取的版本可能不同，因此升级前保留当前镜像版本记录和完整备份。

需要回退时，改回先前的明确版本标签再拉取、重建。若新版本修改了数据格式，同时恢复升级前的完整数据备份；不要用不同备份中的文件拼成一个库。特别是 `0.2.0` 不支持中文名称，创建过中文名称后回退到该版本需要恢复对应的旧备份。

## 8. 备份和恢复

所有应用持久数据都在 `./data`，至少包括：

| 文件 | 用途 |
| --- | --- |
| `master.key` | 解密凭据所需的主密钥 |
| `vault.json` | 加密后的密码、私钥和 Token |
| `state.json` | 管理员密码散列、条目配置、客户端授权和访问设置 |
| `oauth.json` | 内置 OAuth 的持久授权、令牌散列及相关状态；使用后会产生 |
| `first-login.txt` | 仅首次设置前存在的临时管理密码 |

**备份整个目录，不要仅复制 `vault.json`。** 缺失配套主密钥就无法解密；缺失状态文件也无法完整恢复原条目与授权。备份同时包含密文与主密钥，应放在只有自己可访问的位置。

为了得到一致的文件快照，先停止这个服务，复制完整目录后再启动。也应保存当时的 Compose 文件和镜像版本。以下命令在项目目录执行，每次创建新的备份目录：

```sh
docker compose -f compose.fnos.yaml stop nas-mcp
mkdir -p -m 700 ./backups
mcp_backup_dir="./backups/$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$mcp_backup_dir"
sudo cp -a ./data "$mcp_backup_dir/data"
cp ./compose.fnos.yaml "$mcp_backup_dir/compose.fnos.yaml"
docker compose -f compose.fnos.yaml start nas-mcp
```

也可以停止项目后，用飞牛中有权限的账户复制完整 `data` 目录。复制失败时先排查权限并确认备份完整，再进行升级。

恢复顺序：

1. 停止服务，保留当前数据作为另一份副本。
2. 将选中备份的完整 `data` 恢复到 Compose 挂载位置，避免与现有库混合。
3. 确认目录及所有文件的 UID/GID 与 Compose 的 `user` 一致；目录权限为 `700`，数据文件为 `600`。
4. 使用与备份兼容的镜像启动，验证原管理员密码、凭据和客户端授权。

恢复会把客户端授权、撤销记录及 OAuth 刷新令牌状态一起退回备份时间点。**备份之后才撤销的手动 Token 或 OAuth 授权，恢复后可能重新有效；备份之后生成或轮换的令牌也可能失效。** 恢复后应检查“客户端授权”，再次撤销不再需要的访问，必要时让客户端重新连接。OAuth 原有到期时间不会因恢复而延长。变更对外连接地址后，OAuth 客户端也需要按新地址重新连接。

当前实现使用单进程文件存储。不要把同一个数据目录同时挂给多个运行中的实例，也不要增加多 worker 写入；备份恢复与迁移时先停原服务。

## 9. 从旧 Docker 命名卷迁移

旧版可能使用 `nas-mcp-data` 等命名卷。切换成 `./data:/data` 不会自动搬迁数据，直接启动可能得到一个全新的空库。

1. 在飞牛中确认旧容器挂载的实际卷名和路径，并停止旧服务。
2. 先完整备份旧卷，再把卷中的全部文件复制到新项目旁的 `data` 目录。
3. 让目录和文件所有者匹配新 Compose 的 `user`，目录权限为 `700`、文件权限为 `600`。
4. 启动新项目，检查原凭据、管理密码和客户端授权是否都存在。
5. 验证完成前保留旧卷及备份；不要混合旧库和新初始化库的 `master.key`、`vault.json`、`state.json` 或 OAuth 文件。

删除或重建容器不会自动删除 bind 挂载的 NAS 目录，但手动删除 `./data`、清理项目目录或换成其他挂载路径会影响数据。维护时以数据目录为准。

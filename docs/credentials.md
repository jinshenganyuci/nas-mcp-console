# 凭据录入、Token 获取与可用性测试

[返回首页](../README.md) · [接入客户端](clients.md)

先在管理后台保存服务凭据，再给客户端授权。这里录入的 SSH 密码、Cloudflare 密钥、GitHub PAT 等，是客户端最终要使用的服务凭据；「客户端授权」生成的 Token 只用于连接本凭据库，两者不要混填。

## 通用操作

1. 在后台选择「添加凭据」或对应类型的添加按钮。
2. 填写凭据名称、连接信息和秘密值，点击保存。
3. 在条目卡片点击「测试凭据」，查看该次认证结果。
4. 到「客户端授权」把这个条目授予需要使用它的客户端。指定条目的旧授权不会自动包含其他新条目；选择「全部凭据」的授权会包含未来新增条目。

### 凭据名称、备注和用户名分别是什么

| 字段 | 示例 | 用途 |
| --- | --- | --- |
| 凭据名称 | `家里NAS` | 你和 AI 查找条目时使用的名字 |
| 备注 | `客厅里的文件服务器` | 可选的用途说明，方便区分相似条目 |
| 登录用户名 | `root` | 目标服务实际使用的登录账号 |

凭据名称支持中文、英文、数字、短横线和下划线；以文字或数字开头，长度 1～64 个字符，不含空格。保存后名称固定，例如 SSH 条目的 MCP ID 为 `ssh/家里NAS`。需要换名字时，新建条目并重新核对客户端授权，再删除旧条目。

编辑页不会回显已存密码或 Token：**秘密字段留空会保留原值，填写新值才会替换**。切换 SSH 或 Cloudflare 的认证方式时，会移除旧认证方式的秘密字段，需要填写新方式对应的凭据。修改后原测试结果会清除，应重新测试。

## SSH：root＋密码或私钥

### 账号密码

| 后台字段 | 如何填写 |
| --- | --- |
| 服务器地址 | IP 或域名，例如 `192.168.1.20`；不填 `ssh://`，端口另填 |
| SSH 端口 | 通常为 `22`；使用目标服务器实际开放的端口 |
| 用户名 | 实际 SSH 用户，如 `root`、`admin` 或普通账号 |
| 凭据方式 | 选择「账号密码」 |
| SSH 登录密码 | 上述 SSH 用户的密码 |

支持 `root` 密码登录，但目标 SSH 服务必须允许该账号和密码认证。NAS 网页管理密码与 SSH 密码是否相同取决于 NAS 的账号设置，凭据库不会替你启用 SSH 或修改登录策略。

### SSH 私钥

选择「SSH 私钥」，粘贴完整私钥文本，保留开头、结尾和换行；私钥已加密时同时填写「私钥口令」。当前测试支持 RSA、Ed25519、ECDSA。目标账号应已安装相应公钥。

### 首次测试与主机指纹

首次测试会先显示 SSH 主机指纹，此时尚未发送登录密码或私钥认证。通过服务器本机控制台或已有可信连接核对指纹，确认一致后点击「已核对，信任并测试登录」。登录成功后会保存该主机的 `known_hosts` 公钥记录，后续测试继续核对。

服务器重装、主机密钥轮换或地址填错都可能导致指纹不匹配。改变条目的主机或端口会清除旧主机信任，需要重新核对。测试只完成握手和登录，不执行命令。

测试连接从运行凭据库的容器发出；客户端通过 MCP 取到地址后，实际 SSH 连接从客户端的执行环境发出。因此，NAS 能访问的内网 IP，不一定能从异地客户端直接连接；异地使用需要相应的 VPN、路由或对外 SSH 地址。

## Cloudflare：API Token 或 Global API Key

两种方式在后台用「保存哪种凭据」切换。API Token 使用 Bearer 认证；Global API Key 使用账号邮箱与密钥配对，不能将两者互填。

### 获取用户 API Token

1. 登录 Cloudflare，进入 **My Profile → API Tokens → Create Token**。
2. 选择模板或创建自定义 Token，填写名称。
3. 设置操作权限与资源范围，然后确认创建。
4. 复制新生成的秘密值，在本库选择「API Token」并保存。无需添加 `Bearer ` 前缀。

Cloudflare 只在创建时显示完整 Token。它的权限和资源范围分别设置：有 DNS 权限但没有把目标域名加入范围，仍然无法操作该域名。[Cloudflare 创建 Token 文档](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)

常见 DNS 用途可以这样选择：

| 用途 | 权限与范围 |
| --- | --- |
| 查询域名列表 | `Zone → Zone → Read`，并包含目标域名 |
| 查询域名的解析记录 | 再加 `Zone → DNS → Read` |
| 新增、修改、删除解析记录 | DNS 权限选择 `Edit`，并包含目标域名 |

具体接口仍以 Cloudflare 的权限要求为准。[列出域名](https://developers.cloudflare.com/api/resources/zones/methods/list/)、[列出 DNS 记录](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/)

### 获取 Global API Key

在 Cloudflare 的 **My Profile → API Tokens** 页面，下方 **API Keys** 区域找到 **Global API Key**，点击 **View / 查看**，按页面要求完成身份确认并复制。然后在本库选择「Global API Key（总控制密钥）」，填写 **Cloudflare 账号邮箱** 和密钥。[Cloudflare 获取 Global API Key 文档](https://developers.cloudflare.com/fundamentals/api/get-started/keys/)

Global API Key 可访问该用户有权操作的广泛资源，不能像 API Token 一样按用途限制范围。Cloudflare 将其作为旧认证方式，建议新用途优先使用 API Token；本项目保留两者的存储和测试支持。

### 账户级 API Token 的当前测试限制

Cloudflare 还有在 **Manage Account → API Tokens** 创建的账户级 Token。它与用户 API Token 的验证入口不同。当前应用的后台「API Token」测试固定调用 `/user/tokens/verify`，因此**账户级 Token 可能在后台显示测试失败，但仍能调用它获准访问的业务接口**。

账户级 Token 可以按 API Token 类型保存并通过 MCP 获取；当前后台没有 Account ID 配置，也没有账户级自动验证。需要由客户端结合 Account ID 调用 `/accounts/{account_id}/tokens/verify` 或具体业务接口判断，不能仅凭本库测试失败就断定它无效。[账户级 Token 文档](https://developers.cloudflare.com/fundamentals/api/get-started/account-owned-tokens/)、[账户级验证接口](https://developers.cloudflare.com/api/resources/accounts/subresources/tokens/methods/verify/)

后台对用户 API Token 只检查是否处于有效状态；对 Global API Key 只检查当前身份。两者都不会枚举域名、读取解析或尝试修改记录。

## Alist：账号密码

选择「Alist」，填写：

| 后台字段 | 示例或说明 |
| --- | --- |
| Alist 服务地址 | `http://192.168.1.20:5244` 或 `https://files.example.com`；填写服务基础地址 |
| Alist 用户名 | Alist 内创建的用户名 |
| Alist 登录密码 | 对应账号的密码 |

地址不要带登录凭据、查询参数、文件路径或 `/api/auth/login`。测试会在基础地址后附加 `/api/auth/login`，只尝试登录，不读取文件。若服务部署在子路径下，应填写完整的服务基础路径。

当前表单只支持用户名和密码，没有动态 OTP 输入；需要 OTP 的账号可能无法通过后台测试。测试成功也不代表该账号有所有目录的读写权限。

## GitHub：个人访问令牌 PAT

后台填写 **GitHub 用户名** 和 **个人访问令牌（PAT）**。用户名必须是生成此 Token 的个人登录名，例如 `example-user`，不填邮箱、显示名称或组织名。后台会调用 GitHub `/user` 并核对返回的登录名。

### 选哪一种 Token

| 类型 | 适合的用途 |
| --- | --- |
| Fine-grained token（细粒度） | 按指定仓库和具体权限授权，例如只读取某个仓库 |
| Personal access token (classic)（经典） | 需要 classic scope 的工具，或使用个人凭据访问 GHCR |

两者都能作为 GitHub 条目保存。Fine-grained Token 还需要选择资源所有者、仓库范围和具体权限；组织可能要求批准。Token 不能赋予账号原本没有的权限，classic 也可能受到组织策略和 SSO 限制。[GitHub PAT 文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

### 创建 Fine-grained Token

1. 打开 **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**，也可进入 [Fine-grained Token 创建页](https://github.com/settings/personal-access-tokens/new)。
2. 填写名称和有效期；在 **Resource owner** 选择目标仓库所属的个人或组织。
3. 在 **Repository access** 选择范围；若选 **Only select repositories**，还需逐个勾选目标仓库。
4. 在 **Permissions → Repository permissions** 配置具体权限，然后生成并保存到本库。组织需要审批时，等待管理员批准后才能按授权访问其私有资源。[GitHub 创建步骤](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token)

| 用途举例 | Repository permissions |
| --- | --- |
| 读取仓库文件和目录 | `Contents: Read-only` |
| 创建或修改普通仓库文件 | `Contents: Read and write` |
| 删除整个仓库 | `Administration: Read and write`，且账号自身有仓库管理权限 |

修改工作流文件还涉及 `Workflows` 写权限；其他管理操作应核对各接口要求。`Administration` 不能代替文件内容权限。[文件内容接口权限](https://docs.github.com/en/rest/repos/contents)、[删除仓库接口权限](https://docs.github.com/en/rest/repos/repos#delete-a-repository)

即使 Resource owner 选的是组织，本库「GitHub 用户名」仍填写生成 Token 的个人登录名。Fine-grained Token 的权限名称与下方 Classic scope 不同，不需要寻找 `repo` 复选框。

### 创建 Classic Token

1. 在 GitHub 打开 **Settings → Developer settings → Personal access tokens → Tokens (classic)**。
2. 点击 **Generate new token → Generate new token (classic)**。
3. 填写用途名称和有效期，按需要勾选下表中的权限。
4. 点击生成，复制 Token 并保存到本库。需要访问启用 SSO 的组织时，完成 GitHub 要求的 SSO 授权。

也可以直接进入 [GitHub Classic Token 创建页](https://github.com/settings/tokens/new)。

| 用途 | Classic scope |
| --- | --- |
| 读写公开及私有仓库、日常仓库管理 | `repo` |
| 仅操作公开仓库 | `public_repo` 可作为更小范围的选择 |
| 新建或更新 Actions 工作流文件 | 在仓库权限之外加 `workflow` |
| 删除仓库 | `delete_repo` |
| 读取 GHCR 镜像 | `read:packages` |
| 上传和下载 GHCR 镜像 | `write:packages` |
| 删除 GHCR 镜像包 | `delete:packages`，同时具备读取权限及包的管理员权限 |

`repo` 不等于所有权限；例如 `workflow` 和 `delete_repo` 需要另行勾选。组织、账户、SSH/GPG 密钥管理也有各自的 scope，按用途选择。`X-OAuth-Scopes` 可用于检查 classic Token 已授予的范围，部分子权限会合并到父权限中，不能用返回数量判断是否「全选」。[GitHub scope 说明](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)、[Packages 权限](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages)

本库的 GitHub 测试只确认 Token 和登录用户名匹配，不读取仓库列表、不审计全部 scope，也不验证写入或删除权限。需要确认仓库访问时，让客户端取得凭据后执行只读查询；创建仓库、提交代码等实际工作仍由客户端完成。

## Docker Hub 与 GHCR：分别保存镜像仓库凭据

这两个平台使用不同凭据。需要同时使用时，新建两个「镜像仓库」条目，例如 `DockerHub镜像` 和 `GHCR镜像`。

### Docker Hub Token

1. 登录 [Docker Home](https://app.docker.com/)，进入头像菜单 **Account settings → Personal access tokens**。
2. 点击 **Generate new token**，填写说明和有效期。
3. 选择仓库权限并生成，将完整 Token 保存到本库。

| 用途 | Docker PAT 的仓库权限 |
| --- | --- |
| 只拉取公开镜像 | `Repo Public Read-only` |
| 拉取有权访问的公开及私有镜像 | `Repo Read-only` |
| 上传和下载镜像 | `Repo Read & Write` |
| 还需要删除镜像、管理仓库 | `Repo Read, Write, Delete` |

创建界面的名称以 Docker 当前页面为准，部分界面可能简写为 Read、Write、Delete。镜像上传需要写权限，默认的公开只读权限不足以推送。[Docker PAT 创建说明](https://docs.docker.com/security/access-tokens/personal-access-tokens/)、[权限参考](https://docs.docker.com/security/access-tokens/reference/)

在本库中填写：

| 后台字段 | 值 |
| --- | --- |
| 镜像仓库 | `Docker Hub · docker.io` |
| 仓库登录用户名 | Docker ID，例如 `example-user`，不要填邮箱 |
| 命名空间 | 个人用户名或有权推送的组织名 |
| 仓库访问 Token | 刚生成的 Docker Hub PAT |

### GHCR Token

使用 GitHub **Classic PAT**，按用途授予 `read:packages`、`write:packages`、`delete:packages`。其中 `write:packages` 已包含镜像下载和上传；删除还需要相应包的管理员权限。个人凭据访问 GHCR 当前使用 classic Token，不用 fine-grained Token。[GHCR 官方认证说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

在本库中填写：

| 后台字段 | 值 |
| --- | --- |
| 镜像仓库 | `GitHub Container Registry · ghcr.io` |
| 仓库登录用户名 | 生成 Token 的 GitHub 用户名 |
| 命名空间 | 存放镜像的个人用户名或组织名 |
| 仓库访问 Token | 有 Packages 权限的 GitHub Classic PAT |

GitHub 条目和 GHCR 条目可以保存同一个有相应权限的 PAT，也可以分别创建。它们在凭据库里是独立条目，替换其中一个不会自动更新另一个。

镜像地址通常为 `docker.io/命名空间/镜像名:标签` 或 `ghcr.io/命名空间/镜像名:标签`。用户名用于登录，命名空间决定镜像发布到哪个个人或组织名下，两者不一定相同。

后台测试只验证镜像仓库认证，不检查命名空间的读写权限，不连接 Docker Engine，也不构建或推送镜像。拿到 Token 的客户端仍需可用的 Docker 或其他镜像构建、推送工具。

## 如何理解测试结果

| 现象 | 下一步 |
| --- | --- |
| 显示「已保存凭据」 | 仅表示已录入秘密值，点击「测试凭据」才会尝试认证 |
| 测试成功，但业务操作返回 403 | 检查目标资源范围、具体操作权限、组织策略和账号自身权限 |
| GitHub 提示登录账户不匹配 | 把用户名改为 Token 所属个人账号，组织名放在实际操作的仓库目标中 |
| SSH 提示主机指纹不匹配 | 核对目标主机和密钥变更，不能仅凭 IP 相同就认定是原服务器 |
| Alist 测试失败 | 检查基础地址、账号密码、OTP 要求和服务是否能从容器访问 |
| Cloudflare 账户级 Token 测试失败 | 参照上面的账户级 Token 限制，用正确验证接口或业务接口检查 |
| NAS 测试成功，异地客户端仍连不上 SSH / Alist | 检查客户端到目标服务的网络；MCP 取用凭据不会自动打通内网 |

后台测试结果只记录成功与否、简短说明和时间。连接配置和秘密值变更后重新测试；过期或被撤销的上游 Token 需要到对应平台重新生成，再编辑本库条目替换。保持条目名称不变时，原客户端对这个条目的授权会继续有效。

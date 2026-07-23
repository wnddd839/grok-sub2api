<div align="center">

# grok-sub2api

批量注册 Grok 账号，自动导出为 [Sub2API](https://github.com/Wei-Shaw/sub2api) 可导入的 OAuth 数据包；也可直出 [CLIProxyAPI (CPA)](https://github.com/router-for-me/CLIProxyAPI) 本地 auth，或把已有 Sub2 包离线转成 CPA。

<p>
  <a href="https://github.com/Git-creat7/grokRegister-cpa/stargazers"><img src="https://img.shields.io/github/stars/Git-creat7/grokRegister-cpa?style=flat&logo=github" alt="GitHub stars"></a>
  <a href="https://github.com/Git-creat7/grokRegister-cpa/network/members"><img src="https://img.shields.io/github/forks/Git-creat7/grokRegister-cpa?style=flat&logo=github" alt="GitHub forks"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/GUI%20%2B%20CLI-success.svg" alt="GUI + CLI">
  <img src="https://img.shields.io/badge/Sub2API-oauth%20import-orange.svg" alt="Sub2API">
  <img src="https://img.shields.io/badge/CPA-local%20auth-blueviolet.svg" alt="CPA">
  <img src="https://img.shields.io/badge/xAI-Grok-black.svg" alt="Grok">
</p>

[仓库地址](https://github.com/wnddd839/grok-sub2api) · [Sub2API](https://github.com/Wei-Shaw/sub2api) · [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

</div>

---

> 仅用于自动化流程研究、测试环境验证和个人学习。请遵守目标网站服务条款、当地法律法规与第三方服务限制。

## 它做什么

**默认：协议 HTTP 注册**（`register_mode=protocol`，不打开注册页）：

```text
Fetch signup config（进程内缓存）
   → Turnstile mint（屏外 headed Chrome）∥ 建邮 + 发码 + 等码
   → VerifyEmailCode → SignupServerAction → SSO
   → Device Authorization Flow 换 OAuth（无 referrer / 无 bot_flag）
   → 本地 cpa_auth_dir 和/或 远程 CPA Management API
   → probe cli-chat-proxy.grok.com 测活
```

批量（`count≥2`）走 **S/P/C/O 流水线**：S 预 mint、P 建邮等码、C 注册出 SSO、O 做 CPA/写盘；阶段重叠提高吞吐。

可选：`register_mode=browser` 回退旧 UI 注册页（易带 `bot_flag_source`，测活可能 402）。

## 功能

- **协议 HTTP 注册**（默认）：无注册页浏览器；Turnstile 仅屏外 mint；健康 JWT（无 `referrer` / 无 `bot_flag`）
- 批量 **S/P/C/O 流水线** + 进程内 signup config 缓存 + 单号 Turnstile∥建邮并行
- 注册成功后自动入库 CPA（本地目录 / 远程 Management API，可同时开）
- GUI + CLI；Device Flow 换 token（不再强制 `referrer=grok-build`）
- DuckMail / YYDS / Cloudflare / MailNest（Outlook）/ CloudMail 临时邮箱
- 可选 NSFW：批内**纯 HTTP 后台队列**（不冷启浏览器、不抢 Turnstile 代理）；失败进 `nsfw_pending.txt`，可用 `cmd/retry_pending_nsfw.py` 离线补开
- 页面卡住重试、验证码失败换邮箱；browser 模式仍支持浏览器重启与内存清理
- CLI：一次 `Ctrl+C` 安全停止，清理阶段不刷 traceback；再按一次强制中断
- **分叉增强**：保留 Sub2API 批量导出/验活，以及 `proxy_pool` 按出口配额轮换和失败切换

## 环境要求

- Python 3.9+
- Google Chrome 或 Chromium（协议模式仅用于 Turnstile mint；NSFW 批内默认不开浏览器）
- 可用的 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- 能访问 `accounts.x.ai`、临时邮箱 API、`auth.x.ai` / `cli-chat-proxy.grok.com` 的网络
- 使用 Sub2API 导出时，需要本机或远程 [Sub2API](https://github.com/Wei-Shaw/sub2api)

## 快速开始

```bash
git clone https://github.com/wnddd839/grok-sub2api.git
cd grok-sub2api

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
cp config.example.json config.json
# 可选：代理池
cp proxies.txt.example proxies.txt
```

编辑 `config.json`（**不要提交**）。

<!-- 分叉的详细导出示例保留在配置章节，主流程以下游协议文档为准。
### 只出本地 CPA（默认示例方向）

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的临时邮箱API",
  "cloudflare_auth_mode": "none",
  "defaultDomains": "你的收信域名.com",
  "register_count": 10,
  "proxy": "http://127.0.0.1:7890",
  "cpa_auto_add": true,
  "cpa_auth_dir": "./output/cpa",
  "sub2api_auto_add": false
}
```

成功后在 `output/cpa/` 得到：

```text
xai-user1@example.com.json
xai-user2@example.com.json
...
```

把该目录配置为 CPA 的 auth 热加载目录即可。

### 只出 Sub2API 导入包

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的临时邮箱API",
  "cloudflare_auth_mode": "none",
  "defaultDomains": "你的收信域名.com",
  "register_count": 10,
  "proxy": "http://127.0.0.1:7890",
  "cpa_auto_add": false,
  "sub2api_auto_add": true,
  "sub2api_dir": "./output/sub2api",
  "sub2api_batch_size": 10,
  "sub2api_verify": true,
  "sub2api_verify_workers": 3
}
```

成功后在 `output/sub2api/`（或按批次子目录）得到：

```text
sub2api_accounts_001.json
sub2api_accounts_002.json
...
```

到 Sub2API 管理后台 → **导入数据**，选中上述 JSON 即可。

### 运行

```bash
# Windows 双击亦可
start.bat

# 或
python grok_register_ttk.py          # GUI
python grok_register_ttk.py cli      # CLI（仍会开浏览器）
```

## 配置说明

### CLIProxyAPI / CPA

| 配置项 | 说明 |
| --- | --- |
| `cpa_auto_add` | 开启后注册成功自动写 CPA auth（示例默认 `true`） |
| `cpa_auth_dir` | 本地 CPA auth 目录 → `xai-<email>.json`（示例 `./output/cpa`） |
| `cpa_remote_url` | 远程 CPA，如 `http://127.0.0.1:8317`（可选） |
| `cpa_management_key` | Management API 明文密钥（远程上传时必填） |
-->
### Windows 一键启动

1. 按 [DEPLOYMENT.md](DEPLOYMENT.md) 用 Python 3.13 创建 `.venv` 并安装依赖
2. 双击 `start-gui.cmd` 开图形界面，或 `start-cli.cmd` 开命令行（输入 `start` 开始）

## 配置

| 配置项 | 说明 |
| --- | --- |
| `register_mode` | `protocol`（默认，HTTP 协议注册）/ `browser`（旧 UI 注册页） |
| `cpa_auto_add` | 是否注册后 SSO→CPA auth（关则只保存 SSO） |
| `register_workers` | 并发度上限（协议流水线会映射到 P/C/O 等）；browser 模式为浏览器数，默认 1，最大 8 |
| `log_level` | `info`（默认，隐藏 `[Debug]`）/ `debug`（全量日志） |
| `cpa_auth_dir` | 本地 CPA auth 目录；写入 `xai-<email>.json`，可留空 |
| `cpa_remote_url` | 远程 CPA 地址，如 `http://你的CPA地址:8317` |
| `cpa_management_key` | 远程 CPA 管理密钥（`remote-management.secret-key` 明文） |
| `email_provider` | `duckmail` / `yyds` / `cloudflare` / `mailnest` / `cloudmail` |
| `duckmail_api_base` | DuckMail/Mail.tm API 根地址，默认 `https://api.duckmail.sbs`；Mail.tm 填 `https://api.mail.tm` |
| `duckmail_api_key` | DuckMail API Key（`dk_...`）；Mail.tm 公共接口可不填 |
| `mailnest_api_key` | MailNest（迈巢 Outlook）API Key |
| `mailnest_project_code` | MailNest 项目代码，默认 `x-ai001` |
| `yyds_default_domain` | YYDS 固定收信域名；留空则自动选择已验证域名 |
| `cloudmail_url` | CloudMail 站点根地址，不要附加 `/api` |
| `cloudmail_admin_email` | CloudMail 管理员邮箱；也可用环境变量 `CLOUDMAIL_ADMIN_EMAIL` |
| `cloudmail_password` | CloudMail 管理员密码；也可用环境变量 `CLOUDMAIL_PASSWORD` |
| `register_count` | 目标注册数量 |
| `proxy` | 代理；换 token 的 OAuth 请求也走此代理 |
| `enable_nsfw` | 是否在注册过程中后台开启 NSFW，并在批次结束前等待本批结果 |
| `cloudflare_api_base` | Cloudflare 临时邮箱 API 根地址 |
| `cloudflare_api_key` | 默认匿名模式留空；admin 模式填 `ADMIN_PASSWORD` |
| `cloudflare_auth_mode` | `none` / `bearer` / `x-api-key` / `x-admin-auth` / `query-key` |
| `cloudflare_custom_auth` | Worker 全局密码（`PASSWORDS`），注入 `x-custom-auth` |
| `cloudflare_path_*` | domains / accounts / token / messages 路径 |
| `cloudflare_random_subdomain` | 是否创建 `user@随机子域.主域`（需 Worker `RANDOM_SUBDOMAIN_DOMAINS` 包含该主域） |
| `defaultDomains` | Cloudflare / CloudMail 默认收信域名，多个用逗号分隔 |

### 注册模式 / 并发 / NSFW

**`register_mode`**

- `protocol`（默认）：HTTP 协议注册；不打开注册页；Turnstile 仅屏外 mint
- `browser`：旧 UI 注册页（易打上 `bot_flag_source`，probe 可能 402）
- 环境变量覆盖：`GROK_REGISTER_MODE=protocol|browser`

**协议批量流水线（S/P/C/O）**

- `count≥2` 且 `register_mode=protocol` 时默认启用；`count=1` 走单号并行（Turnstile ∥ 建邮等码）
- `GROK_PROTOCOL_PIPELINE=0` 强制关闭流水线；`=1` 时单号也可进流水线
- signup config 进程内缓存，TTL 默认 1200s（`GROK_SIGNUP_CFG_TTL`）

**并发 `register_workers`**

- 协议模式：映射到流水线 P/C/O 等 worker 上限，Turnstile mint 默认 phys=1
- browser 模式：每个 worker 独立 Chrome 用户目录；实际并发不超过注册数量

**连通性检查**

- GUI「连通性检查」或开始注册前自动跑
- 检查项：代理 TCP/出站、邮箱 API、CPA 本地目录/远程 Management API
- 失败默认只警告，不强制拦截开跑

**NSFW**

- SSO 保存后进入单后台队列，不阻塞 CPA 与后续注册
- **批内默认纯 HTTP**（`set_tos` → `set_birth` → `update_nsfw`），不冷启浏览器，避免与 Turnstile 抢代理
- 失败保留 `nsfw_pending.txt`；离线补开：`python cmd/retry_pending_nsfw.py`（可开浏览器）
- 追求最快注册且不需要敏感内容时，可关 `enable_nsfw`

本地目录与远程上传可同时配置；至少配其一才会真正入库。

### Sub2API

| 配置项 | 说明 |
| --- | --- |
| `sub2api_auto_add` | 开启后注册成功自动导出 |
| `sub2api_dir` | 本地导入包输出目录（示例 `./output/sub2api`） |
| `sub2api_batch_size` | 每个 JSON 包账号数（默认 20） |
| `sub2api_verify` | 写包前用 CLI 风格请求验活 |
| `sub2api_verify_workers` | 并行验活线程数 |
| `sub2api_url` | 远程 Sub2API 根地址（可选） |
| `sub2api_token` | 远程管理员 Bearer Token（可选；需有效 JWT） |

远程创建走 `POST /api/v1/admin/accounts`，Token 无效会 `401 INVALID_TOKEN`，**不影响本地写包**。

### 代理 / 代理池

| 配置项 | 说明 |
| --- | --- |
| `proxy` | 单条 HTTP/SOCKS 代理；换 token / 验活也走此代理 |
| `proxy_pool_file` | 代理列表文件（默认 `proxies.txt`，可参考 `proxies.txt.example`） |
| `proxy_pool` | 也可在配置里直接写代理数组 |
| `proxy_accounts_per_ip` | 每个出口成功注册多少个号后轮换（默认 1） |
| `proxy_rotate_on_fail` | 失败时是否换下一个出口（默认 `true`） |

优先级：`proxy_pool` / `proxies.txt` 池子优先；池空时回退到单条 `proxy`。  
使用代理池时建议关掉系统 TUN，避免浏览器实际出口与换 token 出口不一致。

`proxies.txt` 示例：

```text
http://127.0.0.1:7890
http://user:pass@host:port
socks5://user:pass@host:port
```

### 邮箱 / 通用

| 配置项 | 说明 |
| --- | --- |
| `email_provider` | `cloudflare` / `duckmail` / `yyds` / `mailnest` 等 |
| `cloudflare_api_base` | Cloudflare 临时邮箱 API 根地址 |
| `cloudflare_auth_mode` | `none` / `bearer` / `x-api-key` / `x-admin-auth` / `query-key` |
| `cloudflare_api_key` | admin 模式填 `ADMIN_PASSWORD`；匿名留空 |
| `defaultDomains` | 收信域名，多个用逗号分隔 |
| `register_count` | 目标注册数量 |
| `register_workers` | 并行注册工人数 |
| `enable_nsfw` | 注册后尝试开启 NSFW（失败仍会继续导出） |

### Cloudflare 邮箱示例

匿名（默认）：

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的-worker-api-域名",
  "cloudflare_auth_mode": "none",
  "cloudflare_path_accounts": "/api/new_address",
  "cloudflare_path_messages": "/api/mails",
  "defaultDomains": "你的收信域名.com"
}
```

Admin 创建（匿名被 Turnstile 拦时）：

```json
{
  "cloudflare_api_key": "你的 ADMIN_PASSWORD",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address"
}
```

调试：

```bash
python tools/cf_mail_debug.py \
  --api-base "https://你的-worker-api-域名" \
  --auth-mode x-admin-auth \
  --api-key "你的 ADMIN_PASSWORD" \
  --create-path /admin/new_address \
  --domain "你的收信域名.com"
```

## 邮箱服务商与 CPA

```json
{ "cloudflare_custom_auth": "你的全局访问密码" }
```

### MailNest（Outlook 临时邮箱）

[迈巢 MailNest](https://mailnest.top/) 采用项目制。配置 API Key 与项目代码（默认 `x-ai001`）：

```json
{
  "email_provider": "mailnest",
  "mailnest_api_key": "你的 API Key",
  "mailnest_project_code": "x-ai001"
}
```

- API Key：https://mailnest.top/account  
- 项目代码：https://mailnest.top/buy-email（默认可直接用 `x-ai001`）

### YYDS 邮箱固定域名

默认自动选择已验证域名。若要固定收信域名：

```json
{
  "email_provider": "yyds",
  "yyds_default_domain": "你的收信域名.com"
}
```

GUI「YYDS 收信域名」可填；留空则自动选择。

### CloudMail 邮箱

支持自建 [maillab/cloud-mail](https://github.com/maillab/cloud-mail)。程序用管理员接口创建随机地址，公开接口收信，结束后删除地址：

```json
{
  "email_provider": "cloudmail",
  "cloudmail_url": "https://mail.example.com",
  "cloudmail_admin_email": "admin@example.com",
  "cloudmail_password": "你的管理员密码",
  "defaultDomains": "example.com"
}
```

`cloudmail_url` 填站点根地址，不要附加 `/api`。也可用环境变量 `CLOUDMAIL_URL` / `CLOUDMAIL_ADMIN_EMAIL` / `CLOUDMAIL_PASSWORD`（优先于 config）。

## CPA 自动入库

SSO 不是 CPA 凭据。程序会：

1. 用 SSO 走 **Device Authorization Flow** 向 `auth.x.ai` 换 `access_token` / `refresh_token`（**不**注入 `referrer` / `plan`，健康号 JWT 无这些 claim）
2. 组装 `type=xai` 扁平 auth（`cli-chat-proxy.grok.com`）
3. 本地：`cpa_auth_dir` → `xai-<email>.json`（CPA 热加载）
4. 远程：`POST {cpa_remote_url}/v0/management/auth-files?name=...`（需管理密钥）
5. 可选 probe：`cli-chat-proxy.grok.com/v1/responses` 测活（HTTP 200 为健康）

### 本地目录

```json
{
  "cpa_auto_add": true,
  "cpa_auth_dir": "你的CPA auth目录"
}
```

`cpa_auth_dir` 填 CPA 实际监听的 auth 目录路径即可。

### 远程 Management API

```json
{
  "cpa_auto_add": true,
  "cpa_auth_dir": "",
  "cpa_remote_url": "http://你的CPA地址:8317",
  "cpa_management_key": "你的管理密钥明文"
}
```

要求 CPA：`remote-management.allow-remote` 按访问方式配置；密钥为配置里的明文（启动后配置文件可能被写成 bcrypt，上传仍用明文）。

本地与远程可同时开启。日志前缀：`[CPA]`。

## 已有 SSO / Sub2 包时的转换

### SSO → Sub2 / CPA

已有 SSO 时可脱离注册流程：

#### GUI 补转

注册任务停止时，点击主界面的 **补转缺失 SSO**。程序会在仓库目录扫描全部 `accounts_*.txt` 和 `sso_pending.txt`，按邮箱去重，再与远程 CPA 的已有邮箱比较，只转换远程缺失的账号。转换在后台线程运行，不会卡住界面；点击“停止”会在当前账号完成后停止补转。

#### Python 自动扫描

在仓库目录直接运行，无需指定 TXT：

```bash
python sso_to_auth_json.py
```

程序会自动读取当前目录的 `config.json`，扫描 `accounts_*.txt` 与 `sso_pending.txt`。也可指定其他目录和配置：

```bash
python sso_to_auth_json.py --scan-dir /path/to/register-output \
  --config /path/to/register-output/config.json
```

只扫描上述账号文件，不会读取 `requirements.txt`、`mail_credentials.txt` 或其他无关 TXT。

#### 显式指定文件

```bash
# 导出 Sub2API 包
python sso_to_auth_json.py --sso sso_list.txt --sub2api-dir ./output/sub2api

# 上传远程 Sub2API
python sso_to_auth_json.py --sso sso_list.txt \
  --sub2api-url https://你的Sub2API \
  --sub2api-token '管理员JWT'

# 写 CPA 本地目录
python sso_to_auth_json.py --sso sso_list.txt --cpa-auth-dir ./output/cpa
```

`sso_list.txt`：一行一个 SSO、`邮箱----sso`，或 `邮箱----密码----sso`。

配置了远程 CPA 时，批量转换以远程 Management API 返回的邮箱为唯一判重来源：本地 TXT 有、远程 CPA 没有的账号才会转换。没有配置远程 CPA 时，才回退到本地有效 auth JSON 判重。TXT 内重复邮箱也会先去重。

### 为什么用 Device Flow（健康号）

当前默认与测活策略（相对旧版授权码 + `referrer=grok-build`）：

- **SSO 不能直接喂给 CPA。** 需要 `access_token` / `refresh_token`；SSO 只是换 token 的入场券。
- **健康号 JWT 无 `referrer`、无 `bot_flag_source`。** UI 注册页路径容易打上 `bot_flag_source=1`，probe 常 402；协议注册 + Device Flow 出号更稳。
- **不再强制 `referrer=grok-build`。** 旧说明要求授权码注入该 claim；现网健康样式为 `referrer=None`，日志会打印 `access_token 无 referrer（健康样式）`。
- **base_url 仍用 `cli-chat-proxy.grok.com/v1`。** 指向 grok build 免费通道；勿写成空或误指 `api.x.ai/v1`。
- **协议注册避免 bot flag。** `register_mode=protocol` 不走注册页浏览器，降低被标 bot 的概率。

若 CPA 里仍是旧失效号（错误 `base_url`、异常 claim），用独立转换脚本同邮箱重转覆盖 `xai-<email>.json` 即可。

### Sub2API 包 → CPA（离线）

已有 Sub2 导入包 / 账号 JSON 时，可直接转成 CPA 热加载文件（**不重新换 token**）：

```bash
# 单个导入包
python tools/sub2api_to_cpa.py ./sub2api_accounts_001.json -o ./output/cpa

# 整个目录（递归 *.json）
python tools/sub2api_to_cpa.py ./output/sub2api -o ./output/cpa

# 目标已存在同名文件则跳过
python tools/sub2api_to_cpa.py ./output/sub2api -o ./output/cpa --skip-existing
```

支持形态：

- 导入包 `{ "type": "sub2api-data", "accounts": [...] }`
- 单账号 `{ "platform": "grok", "credentials": {...} }`
- 纯 credentials（含 `access_token` / `refresh_token`）

## 输出与安全

| 路径 | 内容 | 是否提交 |
| --- | --- | --- |
| `output/runs/<时间>/` | 每次运行的账号、SSO 与验活结果 | 否 |
| `output/cpa/` | CPA 扁平凭证 `xai-*.json` | 否 |
| `output/sub2api/` | Sub2API 导入包（含 token） | 否 |
| `output/` | 统一运行产物根目录 | 否 |
| `proxies.txt` | 本地代理列表 | 否 |
| `accounts_*.txt` | 邮箱 / 密码 / SSO | 否 |
| `mail_credentials.txt` | 临时邮箱凭证 | 否 |
| `config.json` | 本地密钥与地址 | 否 |
| `config.example.json` | 示例配置 | 是 |
| `proxies.txt.example` | 代理列表示例 | 是 |

请只提交示例文件，用复制出来的 `config.json` / `proxies.txt` 填真实值。  
**不要**把本机域名、API Key、JWT、代理账密、绝对路径写进文档或示例。

## 常见问题

**注册成功了，但没有 CPA / Sub2 文件**  
`[+] 注册成功` 只表示拿到 SSO。若日志出现换 token 失败，说明连不上 `auth.x.ai`（代理/超时/出口被拦）。没有 OAuth token 就不会写导出文件。

## 稳定性

- 协议模式：config 缓存、Turnstile 失败重试、curl_cffi TLS impersonate 降级
- browser 模式：每账号后可重启浏览器；每成功 5 个做内存清理
- 未收到验证码时换邮箱重试；流水线 Q/token 有 TTL，过期丢弃重取
- Device Flow 限流（HTTP 429 `slow_down`）时 SSO 仍写入 accounts / `sso_pending.txt`，可稍后补转

## 常见问题

**CPA 没出现新账号**  
检查 `cpa_auto_add`、`cpa_auth_dir` 或 `cpa_remote_url` + `cpa_management_key`；看 `[CPA]` 日志是否 Device Flow 换 token / 上传成功；本机/服务器能否访问 `auth.x.ai`。

**CPA 目录是空的**  
确认 `cpa_auto_add: true`，且配置了 `cpa_auth_dir` 或远程 CPA；日志应出现 `[CPA] 已写入本地 ...`。

**导入 Sub2API 后账号到期就停用**  
请确认导入包里 `auto_pause_on_expired` 为 `false`（本仓库默认已关闭）。旧账号需在面板手动关掉「过期自动暂停」。

**远程 Sub2API 401 INVALID_TOKEN**  
管理员 Token 无效或不是 JWT；本地写包仍会成功，修好 Token 后再推。

**验活失败 / 426 CLI version**  
升级 Sub2API 到支持 Grok CLI version 头的版本；本仓库验活会带 CLI 风格请求头。

`cpa_remote_url` 填 CPA 实例根地址，不要附带 OpenAI 兼容接口的 `/v1`。程序会自动追加 `/v0/management/auth-files`。

**创建 Cloudflare 邮箱时 curl 超时**

如果当前网络需要代理访问 `workers.dev`，请在 GUI 的“代理”字段或 `config.json` 的 `proxy` 中显式填写代理地址。不要只依赖终端的 `HTTP_PROXY` / `HTTPS_PROXY`，从桌面启动 GUI 时可能不会继承这些环境变量。

**开启 NSFW 时返回 403**

`set_birth_date` 可能被 `grok.com` Cloudflare 拦截。批内只走 HTTP，失败进 `nsfw_pending.txt`，**不影响**账号保存与 CPA。离线补开：`python cmd/retry_pending_nsfw.py`。不需要敏感内容可关 `enable_nsfw`。

**协议模式还会开浏览器吗**  
会短暂开 **屏外 headed Chrome** 做 Turnstile mint（真 headless 易被 CF 拦）。注册页本身不打开。批内 NSFW 默认不再开浏览器。

**NSFW 失败**  
常见为 Cloudflare 拦 `set_birth_date`。账号仍会保存并入库 CPA，失败保留到 `nsfw_pending.txt`。

**402 / 出口异常**  
注册与换 token 尽量走同一代理出口；代理池场景不要开系统 TUN。可用 `tools/diagnose_402_egress.py` 做出口诊断。

**NSFW 超时**  
不影响账号导出；多为上游接口超时或网络问题。

**curl / OpenSSL TLS 报错（创建邮箱）**  
多为本机 `curl_cffi` 与 OpenSSL 冲突或代理干扰。可重装 `curl_cffi`、关掉异常代理后再试。

**CPA 返回 `503 auth_unavailable: no auth available`**  
不是网络超时，而是 CPA 当前没有可用的 xAI auth。检查：auth 是否写入并被热加载、probe 是否 200、账号是否 403/429。free 号走 `cli-chat-proxy` build 通道，额度由上游控制。

**chat 报 `permission-denied` 或 probe 402**  
常见原因：UI 路径带 `bot_flag_source`、错误 `base_url`（应指向 `cli-chat-proxy.grok.com`）、或旧 claim 组合。优先用 **协议注册 + Device Flow** 重注册/重转覆盖 `xai-<email>.json`。

**Device Flow 报 `slow_down` / 429**  
短时间 device code 请求过多。SSO 已在 accounts / pending，稍后 `python sso_to_auth_json.py` 补转即可；适当降低并发或错开 O 阶段。

## 目录结构

```text
.
├── grok_register_ttk.py      # 主程序（GUI / CLI + CPA 入库）
├── protocol_signup.py        # 协议 HTTP 注册 / config 缓存 / mint∥建邮
├── protocol_pipeline.py      # 批量 S/P/C/O 流水线
├── scripts/turnstile_mint.py # Turnstile 屏外 mint
├── browser_session.py        # 浏览器启停 / cf_clearance
├── register_flow.py          # browser 模式注册页填表 / 验证码 / SSO
├── connectivity.py           # 启动前连通性检查
├── nsfw_retry.py             # NSFW pending 队列
├── cmd/retry_pending_nsfw.py # 离线补开 NSFW
├── email_providers/
│   ├── common.py
│   ├── duckmail.py
│   ├── cloudflare.py
│   ├── yyds.py
│   ├── mailnest.py
│   └── cloudmail.py
├── sso_to_auth_json.py       # SSO → CPA / Sub2API（Device Flow）
├── proxy_pool.py             # 代理池轮换
├── config.example.json
├── proxies.txt.example
├── requirements.txt
├── tools/
│   ├── sub2api_to_cpa.py      # Sub2 包 → CPA（离线）
│   ├── cf_mail_debug.py       # Cloudflare 邮箱调试
│   └── diagnose_402_egress.py # 出口 / 402 诊断
├── start-gui.cmd
├── start-cli.cmd
├── DEPLOYMENT.md
├── tests/
├── output/                    # 本地运行产物（不提交）
└── assets/
```

## Star History

<a href="https://www.star-history.com/?type=date&repos=Git-creat7%2FgrokRegister-cpa">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Git-creat7/grokRegister-cpa&type=date&theme=dark&legend=top-left&sealed_token=nLCws8QCmosQswlx1hTjASUcz8r72ZEKXOP1C8WmTFqosF65NL66q77qlMIbBZ6Kqic0cOqA5VisinVcERXNFlwMZqx0ET8872ALY3-k8rvCyvNqa-RxzMLV_oOrrAV54D0E6Pfv4WWTmaA6WYQBr2U5dizobLbasNLXpKTnZZJI7-uRL0zomGISzIGq" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Git-creat7/grokRegister-cpa&type=date&legend=top-left&sealed_token=nLCws8QCmosQswlx1hTjASUcz8r72ZEKXOP1C8WmTFqosF65NL66q77qlMIbBZ6Kqic0cOqA5VisinVcERXNFlwMZqx0ET8872ALY3-k8rvCyvNqa-RxzMLV_oOrrAV54D0E6Pfv4WWTmaA6WYQBr2U5dizobLbasNLXpKTnZZJI7-uRL0zomGISzIGq" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Git-creat7/grokRegister-cpa&type=date&legend=top-left&sealed_token=nLCws8QCmosQswlx1hTjASUcz8r72ZEKXOP1C8WmTFqosF65NL66q77qlMIbBZ6Kqic0cOqA5VisinVcERXNFlwMZqx0ET8872ALY3-k8rvCyvNqa-RxzMLV_oOrrAV54D0E6Pfv4WWTmaA6WYQBr2U5dizobLbasNLXpKTnZZJI7-uRL0zomGISzIGq" />
 </picture>
</a>

## License

[MIT](LICENSE)

## Acknowledgments

- [Sub2API](https://github.com/Wei-Shaw/sub2api)
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [linux.do](https://linux.do)

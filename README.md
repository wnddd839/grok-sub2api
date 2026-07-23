<div align="center">

# grok-sub2api

批量注册 Grok 账号，自动导出为 [Sub2API](https://github.com/Wei-Shaw/sub2api) 可导入的 OAuth 数据包；也可直出 [CLIProxyAPI (CPA)](https://github.com/router-for-me/CLIProxyAPI) 本地 auth，或把已有 Sub2 包离线转成 CPA。

<p>
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

```text
打开注册页 → 临时邮箱收验证码 → 填资料 / 过人机验证
    → 拿到 SSO → 授权码流程换 access_token / refresh_token
    → 验活（可选）
    → 写出 Sub2API 导入包 和/或 本地 CPA xai-*.json
    →（可选）远程创建到 Sub2API / 上传到 CPA Management API
```

两条导出链路互相独立，可只开其一，也可同时开。

## 功能亮点

- **Sub2API 导出**：官方导入包格式（`type: sub2api-data`），按批次分包（默认 20，可配）
- **CPA 本地直出**：默认示例开启，写出可热加载的 `xai-<email>.json`
- **Sub2 → CPA 离线转换**：已有 Sub2 导入包时，无需重新换 token 即可转成 CPA
- **代理池**：支持 `proxies.txt` / 配置数组；按「每 IP 成功 N 个」轮换，失败可自动换出口
- **并行验活**：换 token / 验活 / 写包不阻塞浏览器注册主循环；验活失败不写包
- **过期不停调度**：Sub2 导出账号默认 `auto_pause_on_expired: false`
- **GUI + CLI**：界面改配置；也可用 `start.bat` / CLI
- **临时邮箱**：Cloudflare Temp Mail / DuckMail / YYDS / Mailnest 等
- **稳定性**：浏览器重启、卡住重试、验证码失败换邮箱、`Ctrl+C` 安全停止

## 环境要求

- Python 3.9+（建议 3.12 / 3.13；避免过新的实验版本）
- Google Chrome 或 Chromium
- 能访问注册页、临时邮箱 API、`auth.x.ai`（换 token）
- 使用 Sub2API 时：本机或远程 [Sub2API](https://github.com/Wei-Shaw/sub2api)（建议较新版本，Grok OAuth 需支持 CLI version 头）
- 使用 CPA 时：把 `cpa_auth_dir` 指到 CPA 的 auth 热加载目录，或配置远程 Management API

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

## 已有 SSO / Sub2 包时的转换

### SSO → Sub2 / CPA

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

`sso_list.txt`：一行一个 SSO，或 `邮箱----密码----sso`。

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

**CPA 目录是空的**  
确认 `cpa_auto_add: true`，且配置了 `cpa_auth_dir` 或远程 CPA；日志应出现 `[CPA] 已写入本地 ...`。

**导入 Sub2API 后账号到期就停用**  
请确认导入包里 `auto_pause_on_expired` 为 `false`（本仓库默认已关闭）。旧账号需在面板手动关掉「过期自动暂停」。

**远程 Sub2API 401 INVALID_TOKEN**  
管理员 Token 无效或不是 JWT；本地写包仍会成功，修好 Token 后再推。

**验活失败 / 426 CLI version**  
升级 Sub2API 到支持 Grok CLI version 头的版本；本仓库验活会带 CLI 风格请求头。

**402 / 出口异常**  
注册与换 token 尽量走同一代理出口；代理池场景不要开系统 TUN。可用 `tools/diagnose_402_egress.py` 做出口诊断。

**NSFW 超时**  
不影响账号导出；多为上游接口超时或网络问题。

**curl / OpenSSL TLS 报错（创建邮箱）**  
多为本机 `curl_cffi` 与 OpenSSL 冲突或代理干扰。可重装 `curl_cffi`、关掉异常代理后再试。

## 目录结构

```text
.
├── grok_register_ttk.py       # 主程序 GUI / CLI
├── sso_to_auth_json.py        # SSO → Sub2API / CPA
├── proxy_pool.py              # 代理池轮换
├── start.bat                  # Windows 一键启动
├── config.example.json
├── proxies.txt.example
├── requirements.txt
├── tools/
│   ├── sub2api_to_cpa.py      # Sub2 包 → CPA（离线）
│   ├── cf_mail_debug.py       # Cloudflare 邮箱调试
│   └── diagnose_402_egress.py # 出口 / 402 诊断
├── tests/
├── output/                    # 本地运行产物（不提交）
└── assets/
```

## License

[MIT](LICENSE)

## Acknowledgments

- [Sub2API](https://github.com/Wei-Shaw/sub2api)
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [linux.do](https://linux.do)

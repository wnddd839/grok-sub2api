<div align="center">

# grok-sub2api

批量注册 Grok 账号，自动导出为 [Sub2API](https://github.com/Wei-Shaw/sub2api) 可导入的 OAuth 数据包；可选同步到远程 Sub2API，也兼容 [CLIProxyAPI (CPA)](https://github.com/router-for-me/CLIProxyAPI)。

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/GUI%20%2B%20CLI-success.svg" alt="GUI + CLI">
  <img src="https://img.shields.io/badge/Sub2API-oauth%20import-orange.svg" alt="Sub2API">
  <img src="https://img.shields.io/badge/xAI-Grok-black.svg" alt="Grok">
</p>

[仓库地址](https://github.com/wnddd839/grok-sub2api) · [Sub2API](https://github.com/Wei-Shaw/sub2api) · [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)

</div>

---

> 仅用于自动化流程研究、测试环境验证和个人学习。请遵守目标网站服务条款、当地法律法规与第三方服务限制。

## 它做什么

```text
打开注册页 → 临时邮箱收验证码 → 填资料 / 过人机验证
    → 拿到 SSO → device-flow 换 access_token / refresh_token
    → 验活（可选）→ 按批次写出 Sub2API 导入包
    →（可选）远程创建到 Sub2API / 写入 CPA
```

一次跑完，得到可直接在 Sub2API 后台「导入数据」的 `sub2api-data` JSON 包。

## 功能亮点

- **Sub2API 一等公民**：官方导出包格式（`type: sub2api-data`），按批次分包（默认 20，可配）
- **并行验活**：换 token / `/models` 验活 / 写包不阻塞浏览器注册主循环；验活失败不写包
- **过期不停调度**：导出账号默认 `auto_pause_on_expired: false`，避免 access 到期后无法触发 refresh
- **GUI + CLI**：界面改配置；也可用 `启动注册机.bat` / CLI
- **临时邮箱**：Cloudflare Temp Mail / DuckMail / YYDS
- **可选 CPA**：本地 auth 目录热加载，或 Management API 远程上传
- **稳定性**：浏览器重启、卡住重试、验证码失败换邮箱、`Ctrl+C` 安全停止

## 环境要求

- Python 3.9+（建议 3.12 / 3.13；避免过新的实验版本）
- Google Chrome 或 Chromium
- 能访问注册页、临时邮箱 API、`auth.x.ai`（device-flow 换 token）
- 使用 Sub2API 时：本机或远程 [Sub2API](https://github.com/Wei-Shaw/sub2api)（建议较新版本，Grok OAuth 需支持 CLI version 头）

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
```

编辑 `config.json`（**不要提交**），最小 Sub2API 本地导出示例：

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://你的临时邮箱API",
  "cloudflare_auth_mode": "none",
  "defaultDomains": "你的收信域名.com",
  "register_count": 10,
  "proxy": "http://127.0.0.1:7890",
  "sub2api_auto_add": true,
  "sub2api_dir": "./sub2api_out",
  "sub2api_batch_size": 10,
  "sub2api_verify": true,
  "sub2api_verify_workers": 3
}
```

运行：

```bash
# Windows 双击亦可
启动注册机.bat

# 或
python grok_register_ttk.py          # GUI
python grok_register_ttk.py cli      # CLI（仍会开浏览器）
```

成功后在 `sub2api_out/batch_时间戳/` 下得到：

```text
sub2api_accounts_001.json
sub2api_accounts_002.json
...
```

到 Sub2API 管理后台 → **导入数据**，选中上述 JSON 即可。

## 配置说明

### Sub2API（主推）

| 配置项 | 说明 |
| --- | --- |
| `sub2api_auto_add` | 开启后注册成功自动导出 |
| `sub2api_dir` | 本地导入包输出目录 |
| `sub2api_batch_size` | 每个 JSON 包账号数（默认 20） |
| `sub2api_verify` | 写包前用 CLI 风格请求验活 |
| `sub2api_verify_workers` | 并行验活线程数 |
| `sub2api_url` | 远程 Sub2API 根地址（可选） |
| `sub2api_token` | 远程管理员 Bearer Token（可选；需有效 JWT） |

远程创建走 `POST /api/v1/admin/accounts`，Token 无效会 `401 INVALID_TOKEN`，**不影响本地写包**。

### 邮箱 / 代理 / 通用

| 配置项 | 说明 |
| --- | --- |
| `email_provider` | `cloudflare` / `duckmail` / `yyds` |
| `cloudflare_api_base` | Cloudflare 临时邮箱 API 根地址 |
| `cloudflare_auth_mode` | `none` / `bearer` / `x-api-key` / `x-admin-auth` / `query-key` |
| `cloudflare_api_key` | admin 模式填 `ADMIN_PASSWORD`；匿名留空 |
| `defaultDomains` | 收信域名，多个用逗号分隔 |
| `register_count` | 目标注册数量 |
| `proxy` | HTTP 代理；换 token / 验活也走此代理 |
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
python cf_mail_debug.py \
  --api-base "https://你的-worker-api-域名" \
  --auth-mode x-admin-auth \
  --api-key "你的 ADMIN_PASSWORD" \
  --create-path /admin/new_address \
  --domain "你的收信域名.com"
```

### CLIProxyAPI（可选）

| 配置项 | 说明 |
| --- | --- |
| `cpa_auto_add` | 开启 CPA 入库 |
| `cpa_auth_dir` | 本地 CPA auth 目录 → `xai-<email>.json` |
| `cpa_remote_url` | 远程 CPA，如 `http://你的CPA:8317` |
| `cpa_management_key` | Management API 明文密钥 |

## 已有 SSO 时单独转换

```bash
# 导出 Sub2API 包
python sso_to_auth_json.py --sso sso_list.txt --sub2api-dir ./sub2api_out

# 上传远程 Sub2API
python sso_to_auth_json.py --sso sso_list.txt \
  --sub2api-url https://你的Sub2API \
  --sub2api-token '管理员JWT'

# 写 CPA 本地目录
python sso_to_auth_json.py --sso sso_list.txt --cpa-auth-dir /path/to/auths
```

`sso_list.txt`：一行一个 SSO，或 `邮箱----密码----sso`。

## 输出与安全

| 路径 | 内容 | 是否提交 |
| --- | --- | --- |
| `sub2api_out/` | Sub2API 导入包（含 token） | 否 |
| `auth_out/` | CPA 扁平凭证 | 否 |
| `accounts_*.txt` | 邮箱 / 密码 / SSO | 否 |
| `mail_credentials.txt` | 临时邮箱凭证 | 否 |
| `config.json` | 本地密钥与地址 | 否 |
| `config.example.json` | 示例配置 | 是 |

请只提交 `config.example.json`，用复制出来的 `config.json` 填真实值。

## 常见问题

**注册成功了，但文件夹没有 JSON**  
`[+] 注册成功` 只表示拿到 SSO。若日志出现 `device-flow 换 token 失败，跳过`，说明连不上 `auth.x.ai`（代理/超时）。没有 OAuth token 就不会写 Sub2API 包。

**导入 Sub2API 后账号到期就停用**  
请确认导入包里 `auto_pause_on_expired` 为 `false`（本仓库默认已关闭）。旧账号需在面板手动关掉「过期自动暂停」。

**远程 Sub2API 401 INVALID_TOKEN**  
管理员 Token 无效或不是 JWT；本地写包仍会成功，修好 Token 后再推。

**验活失败 / 426 CLI version**  
升级 Sub2API 到支持 Grok CLI version 头的版本；本仓库验活会带 CLI 风格请求头。

**NSFW 超时**  
不影响账号导出；多为上游接口超时或网络问题。

**curl / OpenSSL TLS 报错（创建邮箱）**  
多为本机 `curl_cffi` 与 OpenSSL 冲突或代理干扰。可重装 `curl_cffi`、关掉异常代理后再试。

## 目录结构

```text
.
├── grok_register_ttk.py       # 主程序 GUI / CLI
├── sso_to_auth_json.py        # SSO → Sub2API / CPA
├── cf_mail_debug.py           # Cloudflare 邮箱调试
├── 启动注册机.bat             # Windows 一键启动
├── config.example.json
├── requirements.txt
├── tests/
└── assets/
```

## License

[MIT](LICENSE)

## Acknowledgments

- [Sub2API](https://github.com/Wei-Shaw/sub2api)
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [linux.do](https://linux.do)

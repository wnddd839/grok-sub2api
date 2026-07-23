#!/usr/bin/env python3
"""Sub2API 导入包 / 账号 JSON → CLIProxyAPI(CPA) 本地 xai-*.json

离线转换，不访问网络、不重新走 SSO 换 token。
适合把已有 Sub2API 导出包交给需要 CPA 热加载目录的客户。

用法:
  # 单个导入包
  python tools/sub2api_to_cpa.py sub2api_accounts_001.json -o ./output/cpa

  # 整个目录（递归 *.json）
  python tools/sub2api_to_cpa.py ./sub2api_out -o ./output/cpa

  # 已存在同名文件则跳过
  python tools/sub2api_to_cpa.py ./sub2api_out -o ./output/cpa --skip-existing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sso_to_auth_json as converter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 Sub2API 格式账号转成 CPA 本地 xai-*.json"
    )
    ap.add_argument(
        "input",
        help="Sub2API JSON 文件或目录（目录会递归扫描 *.json）",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="./output/cpa",
        help="CPA auth 输出目录（默认 ./output/cpa）",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="目标已存在同名 xai-*.json 时跳过",
    )
    args = ap.parse_args(argv)

    try:
        stats = converter.convert_sub2api_path_to_cpa(
            args.input,
            args.output,
            skip_existing=bool(args.skip_existing),
        )
    except Exception as exc:
        print(f"[!] 失败: {exc}", file=sys.stderr)
        return 2

    print(
        f"[*] 完成: ok={stats['ok']} skip={stats['skipped']} fail={stats['fail']} "
        f"→ {Path(args.output).resolve()}"
    )
    for path in stats.get("written") or []:
        print(f"  + {path}")
    for src, err in stats.get("errors") or []:
        print(f"  ! {src}: {err}", file=sys.stderr)

    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate one REF-C screening access code and its Supabase insert statement."""
import hashlib
import hmac
import os
import secrets
import sys


def main() -> None:
    pepper = os.getenv("ACCESS_CODE_PEPPER", "")
    if not pepper:
        raise SystemExit("请先设置环境变量 ACCESS_CODE_PEPPER。")
    name = sys.argv[1] if len(sys.argv) > 1 else "受邀用户"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    code = "refc_" + secrets.token_urlsafe(24)
    digest = hmac.new(pepper.encode(), code.encode(), hashlib.sha256).hexdigest()
    safe_name = name.replace("'", "''")
    print(f"访问码（只显示这一次）：{code}")
    print("请在 Supabase SQL Editor 执行：")
    print(
        "insert into public.screening_users(display_name, access_code_hash, daily_limit) "
        f"values ('{safe_name}', '{digest}', {limit});"
    )


if __name__ == "__main__":
    main()

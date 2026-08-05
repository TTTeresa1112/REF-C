import hashlib
import hmac
from typing import Any, Dict

import requests


class QuotaStoreError(RuntimeError):
    pass


class QuotaStore:
    """Server-side access-code authentication and atomic daily quota accounting."""

    def __init__(self, supabase_url: str, service_key: str, code_pepper: str):
        if not all((supabase_url, service_key, code_pepper)):
            raise QuotaStoreError("邀请码额度服务尚未配置。")
        self.base_url = supabase_url.rstrip("/")
        self.pepper = code_pepper.encode("utf-8")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def code_hash(self, access_code: str) -> str:
        return hmac.new(self.pepper, access_code.strip().encode("utf-8"), hashlib.sha256).hexdigest()

    def authenticate(self, access_code: str) -> Dict[str, Any]:
        if not access_code or len(access_code.strip()) < 12:
            raise QuotaStoreError("访问码无效。")
        response = requests.get(
            f"{self.base_url}/rest/v1/screening_users",
            headers=self.headers,
            params={
                "select": "id,display_name,daily_limit,enabled",
                "access_code_hash": f"eq.{self.code_hash(access_code)}",
                "enabled": "eq.true",
                "limit": "1",
            },
            timeout=15,
        )
        if not response.ok:
            raise QuotaStoreError("邀请码服务暂时不可用。")
        rows = response.json()
        if not rows:
            raise QuotaStoreError("访问码无效或已停用。")
        return rows[0]

    def quota_status(self, user_id: str) -> Dict[str, int]:
        response = requests.post(
            f"{self.base_url}/rest/v1/rpc/get_screening_quota",
            headers=self.headers,
            json={"p_user_id": user_id},
            timeout=15,
        )
        if not response.ok:
            raise QuotaStoreError("无法读取今日额度。")
        data = response.json()
        row = data[0] if isinstance(data, list) else data
        return {key: int(row.get(key, 0)) for key in ("daily_limit", "used", "remaining")}

    def reserve(self, user_id: str, task_id: str, requested_calls: int, filename_hash: str) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/rest/v1/rpc/reserve_screening_quota",
            headers=self.headers,
            json={
                "p_user_id": user_id,
                "p_task_id": task_id,
                "p_requested_calls": int(requested_calls),
                "p_filename_hash": filename_hash,
            },
            timeout=15,
        )
        if not response.ok:
            raise QuotaStoreError("额度预扣失败，请稍后重试。")
        data = response.json()
        row = data[0] if isinstance(data, list) else data
        return row

    def settle(self, task_id: str, actual_calls: int, succeeded: bool = True) -> None:
        response = requests.post(
            f"{self.base_url}/rest/v1/rpc/settle_screening_quota",
            headers=self.headers,
            json={
                "p_task_id": task_id,
                "p_actual_calls": int(actual_calls),
                "p_succeeded": bool(succeeded),
            },
            timeout=15,
        )
        if not response.ok:
            raise QuotaStoreError("额度结算失败，请联系管理员。")

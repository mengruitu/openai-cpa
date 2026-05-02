from curl_cffi import requests
from typing import Any, Optional, Tuple, List, Dict
from utils import config as cfg

class TempMailOrgService:
    BASE_URL = "https://web2.temp-mail.org"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://web2.temp-mail.org",
        "Referer": "https://web2.temp-mail.org",
    }

    def __init__(self, proxies: Optional[Dict[str, str]] = None):
        self.proxies = proxies
        self.session = requests.Session(impersonate="chrome110")
        self.session.headers.update(self.HEADERS)
        self.session.verify = False
        if self.proxies:
            self.session.proxies.update(self.proxies)

    def create_email(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            r = self.session.post(f"{self.BASE_URL}/mailbox", timeout=15)

            if r.status_code == 200:
                data = r.json()
                return data.get("mailbox"), data.get("token")
            else:
                print(f"[{cfg.ts()}] [ERROR] [TempMail.org] 被拦截！状态码: {r.status_code}, 内容: {r.text[:200]}")

        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [TempMail.org] 创建异常 (可能是网络/代理不通): {e}")

        return None, None

    def get_inbox(self, token: str) -> List[dict]:
        try:
            req_headers = {"Cache-Control": "no-cache", "Authorization": f"Bearer {token}"}
            r = self.session.get(f"{self.BASE_URL}/messages", headers=req_headers, timeout=30)

            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    messages = data.get("messages", [])
                    if isinstance(messages, list):
                        return messages
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [TempMail.org] 获取邮件错误: {e}")

        return []

    def get_message(self, token: str, msg_id: str) -> Dict[str, Any]:
        if not token or not msg_id:
            return {}
        try:
            req_headers = {"Cache-Control": "no-cache", "Authorization": f"Bearer {token}"}
            r = self.session.get(f"{self.BASE_URL}/messages/{msg_id}", headers=req_headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, dict) else {}
            print(f"[{cfg.ts()}] [WARNING] [TempMail.org] 获取邮件详情失败: HTTP {r.status_code} {r.text[:160]}")
        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] [TempMail.org] 获取邮件详情异常: {e}")
        return {}

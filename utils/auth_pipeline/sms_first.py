import json
import random
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests

from utils import config as cfg
from utils.auth_core import generate_payload
from utils.email_providers import mail_service
from utils.email_providers.mail_service import get_email_and_token, mask_email

from .common import (
    _extract_next_url,
    _otp_verify_loop,
    _parse_workspace_from_auth_cookie,
    _pkce_verifier,
    _random_state,
    _sha256_b64url_no_pad,
    generate_random_user_info,
)
from .constants import AUTH_URL
from .http_utils import _follow_redirect_chain_local, _oai_headers, _post_with_retry, _skip_net_check, _ssl_verify
from .oauth import generate_oauth_url, submit_callback_url
from .user_utils import _generate_password


CHATGPT_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
CHATGPT_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"
CHATGPT_SCOPE = (
    "openid email profile offline_access model.request model.read "
    "organization.read organization.write"
)


def _normalize_proxy(proxy: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    proxy = cfg.format_docker_url(proxy)
    if proxy and proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://")
    return proxy, {"http": proxy, "https": proxy} if proxy else None


def _generate_chatgpt_oauth_url() -> str:
    code_verifier = _pkce_verifier()
    params = {
        "client_id": CHATGPT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CHATGPT_REDIRECT_URI,
        "scope": CHATGPT_SCOPE,
        "state": _random_state(),
        "code_challenge": _sha256_b64url_no_pad(code_verifier),
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _network_check(session: requests.Session, proxies: Any) -> bool:
    if _skip_net_check():
        return True
    try:
        start = time.time()
        res = session.get(
            "https://cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            verify=_ssl_verify(),
            timeout=10,
        )
        elapsed = time.time() - start
        loc = ""
        for line in str(res.text or "").splitlines():
            if line.startswith("loc="):
                loc = line.split("=", 1)[1].strip()
                break
        if loc in ("CN", "HK"):
            raise RuntimeError(f"当前代理所在地不支持 OpenAI ({loc})")
        print(f"[{cfg.ts()}] [INFO] 节点测活成功！地区: {loc or '未知'} | 延迟: {elapsed:.2f}s")
        return True
    except Exception as e:
        print(f"[{cfg.ts()}] [ERROR] 代理网络检查失败: {e}")
        return False


def _log_openai_phone_otp_channel(resp: Any) -> str:
    """Log whether OpenAI's own send endpoint appears to use SMS text or WhatsApp."""
    raw_status = getattr(resp, "status_code", "未知")
    try:
        status = int(raw_status)
    except Exception:
        status = 0
    location = ""
    response_url = ""
    body = ""

    try:
        location = str(resp.headers.get("location") or resp.headers.get("Location") or "")
    except Exception:
        location = ""
    try:
        response_url = str(getattr(resp, "url", "") or "")
    except Exception:
        response_url = ""
    try:
        body = str(getattr(resp, "text", "") or "")[:2000]
    except Exception:
        body = ""

    probe = "\n".join([location, response_url, body]).lower()
    if any(marker in probe for marker in ("whatsapp", "whats_app", "wa.me", "wa_otp")):
        channel = "WhatsApp"
        confidence = "明确"
    elif 200 <= status < 400:
        channel = "文本短信"
        confidence = "推定"
    else:
        channel = "未知"
        confidence = "未知"

    suffix = f" | location={location}" if location else ""
    print(f"[{cfg.ts()}] [SMS-FIRST] OpenAI 发码通道判断: {channel} ({confidence}, HTTP {raw_status}){suffix}")
    return channel


def _buy_sms_number(proxies: Any) -> tuple[str, str, str, str]:
    if getattr(cfg, "SMSBOWER_ENABLED", False):
        from utils.integrations.smsbower_sms import (
            _smsbower_get_number,
            _smsbower_max_tries,
            _smsbower_pick_country_id,
        )

        service = str(getattr(cfg, "SMSBOWER_SERVICE", "dr") or "dr").strip()
        country = int(getattr(cfg, "SMSBOWER_COUNTRY", 0) or 0)
        excluded = set()
        for attempt in range(1, int(_smsbower_max_tries()) + 1):
            country = _smsbower_pick_country_id(
                proxies,
                service_code=service,
                preferred_country=country,
                exclude_country_ids=excluded,
                force_refresh=attempt > 1,
            )
            aid, phone, err, cost = _smsbower_get_number(proxies, service_code=service, country_id=country)
            if aid and phone:
                print(f"[{cfg.ts()}] [SMS-FIRST] SmsBower 取号成功: {phone} (订单 {aid}, 费用 {cost or '未知'})")
                return "smsbower", aid, phone, ""
            print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] SmsBower 第 {attempt} 次取号失败: {err}")
            excluded.add(country)
        return "smsbower", "", "", "SmsBower 取号失败"

    if getattr(cfg, "HERO_SMS_ENABLED", False):
        from utils.integrations.hero_sms import (
            _hero_sms_get_number,
            _hero_sms_max_tries,
            _hero_sms_pick_country_id,
            _hero_sms_resolve_country_id,
            _hero_sms_resolve_service_code,
        )

        service = _hero_sms_resolve_service_code(proxies)
        preferred = _hero_sms_resolve_country_id(proxies)
        excluded = set()
        country = preferred
        for attempt in range(1, int(_hero_sms_max_tries()) + 1):
            country = _hero_sms_pick_country_id(
                proxies,
                service_code=service,
                preferred_country=preferred,
                exclude_country_ids=excluded,
                force_refresh=attempt > 1,
            )
            aid, phone, err = _hero_sms_get_number(proxies, service_code=service, country_id=country)
            if aid and phone:
                print(f"[{cfg.ts()}] [SMS-FIRST] HeroSMS 取号成功: {phone} (订单 {aid})")
                return "hero_sms", aid, phone, ""
            print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] HeroSMS 第 {attempt} 次取号失败: {err}")
            excluded.add(country)
        return "hero_sms", "", "", "HeroSMS 取号失败"

    if getattr(cfg, "FIVESIM_ENABLED", False):
        from utils.integrations.fivesim_sms import _fivesim_get_number, _fivesim_max_tries, _fivesim_pick_country

        service = str(getattr(cfg, "FIVESIM_SERVICE", "openai") or "openai").strip()
        pref_country = str(getattr(cfg, "FIVESIM_COUNTRY", "any") or "any").strip()
        excluded = set()
        for attempt in range(1, int(_fivesim_max_tries()) + 1):
            country = _fivesim_pick_country(proxies, service, pref_country, excluded)
            aid, phone, err, cost = _fivesim_get_number(proxies, service, country, enable_reuse=False)
            if aid and phone:
                print(f"[{cfg.ts()}] [SMS-FIRST] 5SIM 取号成功: {phone} (订单 {aid}, 费用 {cost})")
                return "fivesim", aid, phone, ""
            print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] 5SIM 第 {attempt} 次取号失败: {err}")
            excluded.add(country)
        return "fivesim", "", "", "5SIM 取号失败"

    return "", "", "", "未开启任何 SMS 供应商"


def _mark_ready(provider: str, activation_id: str, proxies: Any) -> None:
    try:
        if provider == "smsbower":
            from utils.integrations.smsbower_sms import _smsbower_set_status
            _smsbower_set_status(activation_id, 1, proxies)
        elif provider == "hero_sms":
            from utils.integrations.hero_sms import _hero_sms_mark_ready
            _hero_sms_mark_ready(activation_id, proxies)
    except Exception as e:
        print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] 标记接码订单就绪失败，继续尝试: {e}")


def _poll_sms_code(provider: str, activation_id: str, proxies: Any) -> str:
    if provider == "smsbower":
        from utils.integrations.smsbower_sms import _smsbower_poll_code
        return _smsbower_poll_code(activation_id, proxies)
    if provider == "hero_sms":
        from utils.integrations.hero_sms import _hero_sms_poll_code
        return _hero_sms_poll_code(activation_id, proxies)
    if provider == "fivesim":
        from utils.integrations.fivesim_sms import _fivesim_poll_code
        return _fivesim_poll_code(activation_id, proxies, expected_sms_index=0)
    return ""


def _finish_sms_order(provider: str, activation_id: str, proxies: Any, success: bool) -> None:
    try:
        if provider == "smsbower":
            from utils.integrations.smsbower_sms import _smsbower_set_status
            _smsbower_set_status(activation_id, 6 if success else 8, proxies)
        elif provider == "hero_sms":
            from utils.integrations.hero_sms import _hero_sms_set_status
            _hero_sms_set_status(activation_id, 6 if success else 8, proxies)
        elif provider == "fivesim":
            from utils.integrations.fivesim_sms import _fivesim_set_status
            _fivesim_set_status("finish" if success else "ban", activation_id, proxies)
    except Exception as e:
        print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] 更新接码订单状态失败: {e}")


def _extract_continue_url(resp: Any) -> str:
    try:
        data = resp.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    return str(payload.get("url") or data.get("continue_url") or "").strip()


def _error_code(resp: Any) -> str:
    try:
        data = resp.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    return str(err.get("code") or "").strip()


def _write_sms_first_debug_event(
        *,
        stage: str,
        phone: str = "",
        password: str = "",
        provider: str = "",
        activation_id: str = "",
        status: str = "",
        error: str = "",
        extra: dict = None,
) -> None:
    try:
        path = Path("data") / "sms_first_debug_credentials.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "stage": stage,
            "status": status,
            "provider": provider,
            "activation_id": activation_id,
            "phone": phone,
            "password": password,
            "error": error,
        }
        if extra:
            payload["extra"] = extra
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        print(f"[{cfg.ts()}] [WARNING] [SMS-FIRST] 写入调试凭据失败: {e}")


def _get_cookie_value(session: requests.Session, name: str, domains: tuple[str, ...] = ()) -> str:
    jar = getattr(session, "cookies", None)
    if jar is None:
        return ""
    for domain in domains:
        try:
            value = jar.get(name, domain=domain)
            if value:
                return str(value)
        except Exception:
            pass
    try:
        return str(jar.get(name) or "")
    except Exception:
        for cookie in jar:
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", "") or "")
    return ""


def _login_phone_add_email_and_exchange_rt(
        *,
        phone: str,
        password: str,
        email: str,
        email_jwt: str,
        proxy: Optional[str],
        proxies: Any,
        debug_fail,
) -> Optional[str]:
    s_log = requests.Session(proxies=proxies, impersonate="chrome110")
    s_log.headers.update({"Connection": "close"})
    s_log.cookies.clear()
    s_log.timeout = 30
    processed_mails: set = set()
    try:
        oauth_log = generate_oauth_url()
        _, current_url = _follow_redirect_chain_local(s_log, oauth_log.auth_url, proxies)
        if "code=" in current_url and "state=" in current_url:
            return submit_callback_url(
                callback_url=current_url,
                expected_state=oauth_log.state,
                code_verifier=oauth_log.code_verifier,
                redirect_uri=oauth_log.redirect_uri,
                proxies=proxies,
            )

        did = _get_cookie_value(s_log, "oai-did", ("auth.openai.com", ".openai.com", "chatgpt.com", ".chatgpt.com"))
        if not did:
            did = str(uuid.uuid4())
            s_log.cookies.set("oai-did", did, domain="auth.openai.com", path="/")
        current_ua = _oai_headers(did).get("user-agent", "")
        log_ctx = {"session_id": str(uuid.uuid4())}

        print(f"[{cfg.ts()}] [SMS-FIRST] 第二段登录手机号账号，准备补邮箱并换 RT...")
        sentinel_start = generate_payload(
            did=did,
            flow="authorize_continue",
            proxy=proxy,
            user_agent=current_ua,
            impersonate="chrome110",
            ctx=log_ctx,
        )
        start_headers = _oai_headers(did, {
            "Referer": current_url or "https://auth.openai.com/log-in",
            "content-type": "application/json",
        })
        if sentinel_start:
            start_headers["openai-sentinel-token"] = sentinel_start
        start_resp = _post_with_retry(
            s_log,
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=start_headers,
            json_body={
                "username": {"value": phone, "kind": "phone_number"},
                "screen_hint": "login",
            },
            proxies=proxies,
            allow_redirects=False,
        )
        if start_resp.status_code != 200:
            err_msg = f"HTTP {start_resp.status_code} {str(start_resp.text or '')[:360]}"
            debug_fail("login_phone_init", err_msg, {"email": email})
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 登录手机号初始化失败: {err_msg}")
            return None

        pwd_page_url = _extract_continue_url(start_resp) or _extract_next_url(start_resp.json())
        if pwd_page_url:
            _, current_url = _follow_redirect_chain_local(s_log, pwd_page_url, proxies)

        sentinel_pwd = generate_payload(
            did=did,
            flow="password_verify",
            proxy=proxy,
            user_agent=current_ua,
            impersonate="chrome110",
            ctx=log_ctx,
        )
        pwd_headers = _oai_headers(did, {
            "Referer": current_url or "https://auth.openai.com/log-in/password",
            "content-type": "application/json",
        })
        if sentinel_pwd:
            pwd_headers["openai-sentinel-token"] = sentinel_pwd
        pwd_resp = _post_with_retry(
            s_log,
            "https://auth.openai.com/api/accounts/password/verify",
            headers=pwd_headers,
            json_body={"password": password},
            proxies=proxies,
        )
        if pwd_resp.status_code != 200:
            err_msg = f"HTTP {pwd_resp.status_code} {str(pwd_resp.text or '')[:360]}"
            debug_fail("login_password_verify", err_msg, {"email": email})
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 登录密码校验失败: {err_msg}")
            return None

        next_url = _extract_continue_url(pwd_resp) or _extract_next_url(pwd_resp.json())
        if "code=" in next_url and "state=" in next_url:
            return submit_callback_url(
                callback_url=next_url,
                expected_state=oauth_log.state,
                code_verifier=oauth_log.code_verifier,
                redirect_uri=oauth_log.redirect_uri,
                proxies=proxies,
            )

        if "/add-email" in next_url:
            print(f"[{cfg.ts()}] [SMS-FIRST] 登录后进入补邮箱: {mask_email(email)}")
            _, current_url = _follow_redirect_chain_local(s_log, next_url, proxies)
            sentinel_add_email = generate_payload(
                did=did,
                flow="authorize_continue",
                proxy=proxy,
                user_agent=current_ua,
                impersonate="chrome110",
                ctx=log_ctx,
            )
            add_headers = _oai_headers(did, {
                "Referer": current_url or "https://auth.openai.com/add-email",
                "content-type": "application/json",
            })
            if sentinel_add_email:
                add_headers["openai-sentinel-token"] = sentinel_add_email
            add_resp = _post_with_retry(
                s_log,
                "https://auth.openai.com/api/accounts/add-email/send",
                headers=add_headers,
                json_body={"email": email},
                proxies=proxies,
            )
            if add_resp.status_code != 200:
                err_msg = f"HTTP {add_resp.status_code} {str(add_resp.text or '')[:360]}"
                debug_fail("add_email_send", err_msg, {"email": email})
                print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 补邮箱发送 OTP 失败: {err_msg}")
                return None
            next_url = _extract_continue_url(add_resp) or _extract_next_url(add_resp.json())

        if "email-verification" in next_url or "email-otp" in next_url:
            print(f"[{cfg.ts()}] [SMS-FIRST] 开始验证补绑定邮箱 OTP: {mask_email(email)}")
            _, current_url = _follow_redirect_chain_local(s_log, next_url, proxies)
            code, code_resp = _otp_verify_loop(
                session=s_log,
                email=email,
                email_jwt=email_jwt,
                did=did,
                current_ua=current_ua,
                proxy=proxy,
                proxies=proxies,
                ctx=log_ctx,
                processed_mails=processed_mails,
                referer="https://auth.openai.com/email-verification",
                resend_url="https://auth.openai.com/api/accounts/email-otp/resend",
                validate_url="https://auth.openai.com/api/accounts/email-otp/validate",
                flow="authorize_continue",
            )
            if not code or code_resp is None or code_resp.status_code != 200:
                err_msg = "邮箱 OTP 验证失败"
                if code_resp is not None:
                    err_msg = f"HTTP {code_resp.status_code} {str(code_resp.text or '')[:240]}"
                debug_fail("add_email_otp", err_msg, {"email": email})
                print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 补邮箱 OTP 验证失败")
                return None
            next_url = _extract_continue_url(code_resp) or _extract_next_url(code_resp.json())

        if next_url:
            _, current_url = _follow_redirect_chain_local(s_log, next_url, proxies)
        else:
            current_url = ""

        for _ in range(3):
            if "code=" in current_url and "state=" in current_url:
                token_json = submit_callback_url(
                    callback_url=current_url,
                    expected_state=oauth_log.state,
                    code_verifier=oauth_log.code_verifier,
                    redirect_uri=oauth_log.redirect_uri,
                    proxies=proxies,
                )
                print(f"[{cfg.ts()}] [SUCCESS] [SMS-FIRST] Codex callback 换 RT 成功")
                return token_json

            if current_url.endswith("/consent") or current_url.endswith("/workspace"):
                auth_cookie = _get_cookie_value(s_log, "oai-client-auth-session", ("auth.openai.com", ".auth.openai.com"))
                workspaces = _parse_workspace_from_auth_cookie(auth_cookie)
                if not workspaces:
                    debug_fail("workspace_parse", f"未从会话 cookie 解析到 workspace: {current_url}", {"email": email})
                    print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 未从会话中解析到 workspace")
                    return None
                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if not workspace_id:
                    debug_fail("workspace_parse", "workspace_id 为空", {"email": email})
                    return None
                select_resp = _post_with_retry(
                    s_log,
                    "https://auth.openai.com/api/accounts/workspace/select",
                    headers=_oai_headers(did, {
                        "Referer": current_url,
                        "content-type": "application/json",
                    }),
                    json_body={"workspace_id": workspace_id},
                    proxies=proxies,
                )
                if select_resp.status_code != 200:
                    err_msg = f"HTTP {select_resp.status_code} {str(select_resp.text or '')[:360]}"
                    debug_fail("workspace_select", err_msg, {"email": email, "workspace_id": workspace_id})
                    print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] workspace 选择失败: {err_msg}")
                    return None
                final_url = _extract_continue_url(select_resp) or _extract_next_url(select_resp.json())
                _, current_url = _follow_redirect_chain_local(s_log, final_url, proxies)
                continue

            break

        debug_fail("login_callback", f"未拿到 OAuth callback: {current_url}", {"email": email})
        print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 登录补邮箱后未拿到 OAuth callback: {current_url}")
        return None
    finally:
        try:
            s_log.close()
        except Exception:
            pass


def run_sms_first(proxy: Optional[str], run_ctx: dict = None) -> tuple:
    proxy, proxies = _normalize_proxy(proxy)
    side_proxies = cfg.get_next_side_proxies() if hasattr(cfg, "get_next_side_proxies") else None
    if side_proxies:
        side_proxy_label = side_proxies.get("https") or side_proxies.get("http") or ""
        print(f"[{cfg.ts()}] [SMS-FIRST] 接码平台/邮箱使用旁代理: {mask_email(side_proxy_label)}")
    else:
        side_proxies = proxies
    sms_provider = ""
    activation_id = ""
    phone = ""
    email = ""
    email_jwt = ""
    password = ""
    processed_mails: set = set()
    session = None
    order_finished = False
    try:
        session = requests.Session(proxies=proxies, impersonate="chrome110")
        session.headers.update({"Connection": "close"})
        session.timeout = 30

        if not _network_check(session, proxies):
            return None, None

        sms_provider, activation_id, phone, err = _buy_sms_number(side_proxies)
        if not activation_id or not phone:
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] {err}")
            if run_ctx is not None:
                run_ctx["phone_verify"] = True
            return None, None

        mail_service.set_last_email(phone)
        if run_ctx is not None:
            run_ctx["account_identifier"] = phone
            run_ctx["sms_first"] = True

        password = _generate_password()
        _write_sms_first_debug_event(
            stage="created",
            status="pending",
            provider=sms_provider,
            activation_id=activation_id,
            phone=phone,
            password=password,
        )

        def _debug_fail(stage: str, error: str, extra: dict = None) -> None:
            _write_sms_first_debug_event(
                stage=stage,
                status="failed",
                provider=sms_provider,
                activation_id=activation_id,
                phone=phone,
                password=password,
                error=error,
                extra=extra,
            )

        print(f"[{cfg.ts()}] [SMS-FIRST] 第一段使用 ChatGPT 网页端会话创建手机号账号...")
        _, current_url = _follow_redirect_chain_local(session, _generate_chatgpt_oauth_url(), proxies)

        did = _get_cookie_value(session, "oai-did", ("auth.openai.com", ".openai.com", "chatgpt.com", ".chatgpt.com"))
        if not did:
            did = str(uuid.uuid4())
            session.cookies.set("oai-did", did, domain="auth.openai.com", path="/")
        current_ua = _oai_headers(did).get("user-agent", "")

        sms_ctx = {}
        print(f"[{cfg.ts()}] [SMS-FIRST] 提交手机号注册信息: {phone} (密码: {password[:4]}****)")
        sentinel_start = generate_payload(
            did=did,
            flow="authorize_continue",
            proxy=proxy,
            user_agent=current_ua,
            impersonate="chrome110",
            ctx=sms_ctx,
        )
        start_headers = _oai_headers(did, {
            "Referer": current_url or "https://auth.openai.com/create-account",
            "content-type": "application/json",
        })
        if sentinel_start:
            start_headers["openai-sentinel-token"] = sentinel_start
        start_resp = _post_with_retry(
            session,
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=start_headers,
            json_body={
                "username": {"value": phone, "kind": "phone_number"},
                "screen_hint": "login_or_signup",
            },
            proxies=proxies,
        )
        if start_resp.status_code != 200:
            err_msg = f"HTTP {start_resp.status_code} {str(start_resp.text or '')[:240]}"
            _debug_fail("phone_init", err_msg)
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 手机号初始化失败: {err_msg}")
            return None, None
        start_next_url = _extract_continue_url(start_resp)
        if start_next_url:
            print(f"[{cfg.ts()}] [SMS-FIRST] 手机号初始化下一步: {start_next_url}")
        if start_next_url and "create-account/password" not in start_next_url:
            _debug_fail("phone_init_next", f"unexpected continue_url: {start_next_url}")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 手机号初始化后未进入创建密码页: {start_next_url}")
            return None, None

        sentinel_pwd = generate_payload(
            did=did,
            flow="username_password_create",
            proxy=proxy,
            user_agent=current_ua,
            impersonate="chrome110",
            ctx=sms_ctx,
        )
        pwd_headers = _oai_headers(did, {
            "Referer": "https://auth.openai.com/create-account/password",
            "content-type": "application/json",
        })
        if sentinel_pwd:
            pwd_headers["openai-sentinel-token"] = sentinel_pwd
        pwd_resp = _post_with_retry(
            session,
            "https://auth.openai.com/api/accounts/user/register",
            headers=pwd_headers,
            json_body={"password": password, "username": phone},
            proxies=proxies,
        )
        if pwd_resp.status_code != 200:
            err_msg = f"HTTP {pwd_resp.status_code} {pwd_resp.text[:240]}"
            _debug_fail("password_register", err_msg)
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 设置密码失败: {err_msg}")
            if run_ctx is not None:
                run_ctx["pwd_blocked"] = True
            return None, None

        next_url = _extract_continue_url(pwd_resp)
        if "phone-otp/send" not in next_url:
            _debug_fail("password_register_next", f"unexpected continue_url: {next_url}")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 设置密码后未进入短信发送页: {next_url}")
            return None, None

        _mark_ready(sms_provider, activation_id, side_proxies)
        print(f"[{cfg.ts()}] [SMS-FIRST] 请求 OpenAI 发送短信验证码...")
        send_resp = session.get(
            "https://auth.openai.com/api/accounts/phone-otp/send",
            headers=_oai_headers(did, {"Referer": "https://auth.openai.com/create-account/password"}),
            proxies=proxies,
            verify=_ssl_verify(),
            timeout=30,
            allow_redirects=False,
        )
        _log_openai_phone_otp_channel(send_resp)
        if send_resp.status_code not in (200, 302):
            err_msg = f"HTTP {send_resp.status_code} {send_resp.text[:240]}"
            _debug_fail("phone_otp_send", err_msg)
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 短信发送失败: {err_msg}")
            return None, None

        sms_code = _poll_sms_code(sms_provider, activation_id, side_proxies)
        if not sms_code:
            _debug_fail("sms_poll", "接码超时或未获取验证码")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 接码超时或未获取验证码")
            return None, None

        sentinel_verify = generate_payload(
            did=did,
            flow="authorize_continue",
            proxy=proxy,
            user_agent=current_ua,
            impersonate="chrome110",
            ctx=sms_ctx,
        )
        verify_headers = _oai_headers(did, {
            "Referer": "https://auth.openai.com/contact-verification",
            "content-type": "application/json",
        })
        if sentinel_verify:
            verify_headers["openai-sentinel-token"] = sentinel_verify
        verify_resp = _post_with_retry(
            session,
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            headers=verify_headers,
            json_body={"code": sms_code},
            proxies=proxies,
        )
        if verify_resp.status_code != 200:
            err_msg = f"HTTP {verify_resp.status_code} {str(verify_resp.text or '')[:240]}"
            _debug_fail("phone_otp_validate", err_msg)
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 手机验证码校验失败: {err_msg}")
            return None, None

        _finish_sms_order(sms_provider, activation_id, side_proxies, True)
        order_finished = True

        about_url = _extract_continue_url(verify_resp)
        if not about_url.endswith("/about-you"):
            _debug_fail("phone_otp_validate_next", f"unexpected continue_url: {about_url}")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 手机验证后未进入 about-you: {about_url}")
            return None, None

        if run_ctx is not None:
            run_ctx["phone"] = phone

        user_info = generate_random_user_info()
        print(
            f"[{cfg.ts()}] [SMS-FIRST] 初始化账户信息 "
            f"(昵称: {user_info['name']}, 生日: {user_info['birthdate']})..."
        )
        create_resp = None

        def _submit_create_account(body: dict) -> Any:
            last_resp = None
            for create_flow in ("oauth_create_account", "create_account"):
                sentinel_create = generate_payload(
                    did=did,
                    flow=create_flow,
                    proxy=proxy,
                    user_agent=current_ua,
                    impersonate="chrome110",
                    ctx=sms_ctx,
                )
                create_headers = _oai_headers(did, {
                    "Referer": "https://auth.openai.com/about-you",
                    "content-type": "application/json",
                })
                if sentinel_create:
                    create_headers["openai-sentinel-token"] = sentinel_create
                last_resp = _post_with_retry(
                    session,
                    "https://auth.openai.com/api/accounts/create_account",
                    headers=create_headers,
                    json_body=body,
                    proxies=proxies,
                )
                if last_resp.status_code == 200:
                    break
                print(
                    f"[{cfg.ts()}] [WARNING] [SMS-FIRST] 创建资料失败(flow={create_flow}): "
                    f"HTTP {last_resp.status_code} {str(last_resp.text or '')[:360]}"
                )
                if _error_code(last_resp) == "missing_email":
                    break
            return last_resp

        create_resp = _submit_create_account(user_info)
        if create_resp is None:
            _debug_fail("create_account", "创建资料未返回响应")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 创建资料未返回响应")
            return None, None
        if create_resp.status_code != 200:
            err_msg = f"HTTP {create_resp.status_code} {str(create_resp.text or '')[:360]}"
            _debug_fail("create_account", err_msg, {"email": email} if email else None)
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 创建资料失败: {err_msg}")
            return None, None

        callback_url = _extract_continue_url(create_resp)
        if callback_url and "code=" not in callback_url and "state=" in callback_url:
            _, followed_callback = _follow_redirect_chain_local(session, callback_url, proxies)
            if followed_callback:
                callback_url = followed_callback
        if "code=" not in callback_url or "state=" not in callback_url:
            _debug_fail("web_create_callback", f"网页端 about-you 后未返回 callback: {callback_url}")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 网页端 about-you 后未返回 callback: {callback_url}")
            return None, None

        print(f"[{cfg.ts()}] [SUCCESS] [SMS-FIRST] 手机号账号创建成功，准备重新登录补邮箱并换 RT...")
        try:
            session.close()
        except Exception:
            pass
        session = None

        email, email_jwt = get_email_and_token(side_proxies)
        if not email:
            _debug_fail("second_phase_email_get", "手机号账号已创建，但获取补绑定邮箱失败")
            print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 手机号账号已创建，但获取补绑定邮箱失败")
            return None, None
        mail_service.set_last_email(phone)

        token_json = _login_phone_add_email_and_exchange_rt(
            phone=phone,
            password=password,
            email=email,
            email_jwt=email_jwt,
            proxy=proxy,
            proxies=proxies,
            debug_fail=_debug_fail,
        )
        if not token_json:
            return None, None
        token_data = json.loads(token_json)
        token_data["email"] = phone
        token_data["phone"] = phone
        token_data["registration_strategy"] = "sms_first"
        _write_sms_first_debug_event(
            stage="success",
            status="success",
            provider=sms_provider,
            activation_id=activation_id,
            phone=phone,
            password=password,
        )
        print(f"[{cfg.ts()}] [SUCCESS] [SMS-FIRST] 手机号注册并换取 RT 成功: {phone}")
        return json.dumps(token_data, ensure_ascii=False, separators=(",", ":")), password
    except Exception as e:
        if phone and password:
            _write_sms_first_debug_event(
                stage="exception",
                status="failed",
                provider=sms_provider,
                activation_id=activation_id,
                phone=phone,
                password=password,
                error=str(e),
            )
        print(f"[{cfg.ts()}] [ERROR] [SMS-FIRST] 流程异常: {e}")
        return None, None
    finally:
        if activation_id and sms_provider and not order_finished:
            _finish_sms_order(sms_provider, activation_id, side_proxies, False)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

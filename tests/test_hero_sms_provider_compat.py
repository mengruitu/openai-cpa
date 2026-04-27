import io
import sys
import types
import unittest
from contextlib import ExitStack
from contextlib import redirect_stdout
from unittest.mock import patch

fake_requests_module = types.SimpleNamespace(get=None, post=None, Session=object)
sys.modules.setdefault("curl_cffi", types.SimpleNamespace(requests=fake_requests_module))
sys.modules.setdefault(
    "utils.db_manager",
    types.SimpleNamespace(
        get_sys_kv=lambda *args, **kwargs: None,
        set_sys_kv=lambda *args, **kwargs: None,
    ),
)
sys.modules.setdefault(
    "utils.auth_core",
    types.SimpleNamespace(generate_payload=lambda *args, **kwargs: ""),
)

from utils.integrations import hero_sms


class HeroSmsProviderCompatTests(unittest.TestCase):
    def tearDown(self):
        hero_sms._HERO_SMS_COUNTRY_NAMES_MAP.clear()
        hero_sms._HERO_SMS_COUNTRY_NAME_CACHE.clear()
        hero_sms._HERO_SMS_COUNTRY_ENG_NAME_CACHE.clear()
        hero_sms._HERO_SMS_PRICE_V3_CACHE["service"] = ""
        hero_sms._HERO_SMS_PRICE_V3_CACHE["updated_at"] = 0.0
        hero_sms._HERO_SMS_PRICE_V3_CACHE["items"] = []

    def test_balance_returns_provider_message_on_business_error(self):
        with patch(
            "utils.integrations.hero_sms._hero_sms_request",
            return_value=(True, '{"status":"0","message":"No access","data":[]}', {
                "status": "0",
                "message": "No access",
                "data": [],
            }),
        ):
            with redirect_stdout(io.StringIO()):
                balance, err = hero_sms.hero_sms_get_balance()

        self.assertEqual(-1.0, balance)
        self.assertEqual("No access", err)

    def test_country_name_map_supports_wrapped_data_list(self):
        wrapped_payload = {
            "status": "1",
            "message": "ok",
            "data": [
                {"id": "16", "eng": "United Kingdom", "chn": "英国"},
                {"id": "187", "eng": "United States", "chn": "美国"},
            ],
        }
        with patch(
            "utils.integrations.hero_sms._hero_sms_request",
            return_value=(True, "", wrapped_payload),
        ):
            with redirect_stdout(io.StringIO()):
                mapping = hero_sms._get_country_names_map(proxies=None)

        self.assertEqual("英国", mapping[16])
        self.assertEqual("美国", mapping[187])

    def test_prices_v3_parses_country_provider_tree(self):
        payload = {
            "50": {
                "dr": {
                    "2442": {"count": 25860, "price": 0.167, "provider_id": 2442},
                    "2266": {"count": 1, "price": 0.054, "provider_id": 2266},
                }
            }
        }
        with patch("utils.integrations.hero_sms._hero_sms_request", return_value=(True, "", payload)), \
                patch("utils.integrations.hero_sms._get_country_names_map", return_value={50: "奥地利"}), \
                patch("utils.integrations.hero_sms._get_hero_country_eng_names", return_value={50: "Austria"}):
            with redirect_stdout(io.StringIO()):
                rows = hero_sms._hero_sms_prices_v3_by_service("dr", proxies=None, force_refresh=True)

        self.assertEqual(1, len(rows))
        self.assertEqual(50, rows[0]["country"])
        self.assertEqual("奥地利", rows[0]["name"])
        self.assertEqual("Austria", rows[0]["eng_name"])
        self.assertEqual(0.054, rows[0]["min_cost"])
        self.assertEqual(25861, rows[0]["total_count"])
        self.assertEqual([2266, 2442], [x["provider_id"] for x in rows[0]["providers"]])

    def test_get_number_sends_provider_ids_list(self):
        captured = {}

        def fake_request(action, *, proxies, params=None, timeout=25):
            captured["action"] = action
            captured["params"] = dict(params or {})
            return True, "ACCESS_NUMBER:12345:15551234567", None

        with patch.object(hero_sms.cfg, "HERO_SMS_BASE_URL", "https://hero-sms.com/stubs/handler_api.php"), \
                patch("utils.integrations.hero_sms._hero_sms_country_label", return_value="国家50"), \
                patch("utils.integrations.hero_sms.hero_sms_get_balance", return_value=(9.0, "")), \
                patch("utils.integrations.hero_sms._hero_sms_request", side_effect=fake_request):
            with redirect_stdout(io.StringIO()):
                activation_id, phone, err = hero_sms._hero_sms_get_number(
                    proxies=None,
                    service_code="dr",
                    country_id=50,
                    provider_ids=[2442, 2266],
                )

        self.assertEqual("12345", activation_id)
        self.assertEqual("+15551234567", phone)
        self.assertEqual("", err)
        self.assertEqual("getNumber", captured["action"])
        self.assertEqual("2442,2266", captured["params"]["providerIds"])

    def test_get_number_prefers_v2_for_smsbower_and_sends_min_price(self):
        calls = []

        def fake_request(action, *, proxies, params=None, timeout=25):
            calls.append((action, dict(params or {})))
            if action == "getNumberV2":
                return True, "", {
                    "activationId": "9988",
                    "phoneNumber": "628123456789",
                    "activationCost": "0.004",
                }
            return False, "should_not_reach", None

        fake_cfg = {
            "hero_sms": {
                "base_url": "https://smsbower.page/stubs/handler_api.php",
                "min_price": 0.004,
            }
        }
        with patch.object(hero_sms.cfg, "_c", fake_cfg), \
                patch.object(hero_sms.cfg, "HERO_SMS_BASE_URL", "https://smsbower.page/stubs/handler_api.php"), \
                patch("utils.integrations.hero_sms._hero_sms_country_label", return_value="印度尼西亚(6)"), \
                patch("utils.integrations.hero_sms.hero_sms_get_balance", return_value=(9.0, "")), \
                patch("utils.integrations.hero_sms._hero_sms_request", side_effect=fake_request):
            with redirect_stdout(io.StringIO()):
                activation_id, phone, err = hero_sms._hero_sms_get_number(
                    proxies=None,
                    service_code="dr",
                    country_id=6,
                    provider_ids=[3061, 3001],
                )

        self.assertEqual("9988", activation_id)
        self.assertEqual("+628123456789", phone)
        self.assertEqual("", err)
        number_calls = [call for call in calls if call[0].startswith("getNumber")]
        self.assertEqual("getNumberV2", number_calls[0][0])
        self.assertEqual("3061,3001", number_calls[0][1]["providerIds"])
        self.assertEqual(0.004, number_calls[0][1]["minPrice"])

    def test_build_number_candidates_uses_country_and_provider_priority(self):
        fake_cfg = {
            "hero_sms": {
                "selected_countries": [50, 16],
                "country_priority": [16, 50],
                "country_provider_map": {
                    "50": [2442, 2266],
                    "16": [3237, 2442],
                },
                "provider_priority_map": {
                    "50": [2266, 2442],
                    "16": [2442, 3237],
                },
            }
        }
        with patch.object(hero_sms.cfg, "_c", fake_cfg), \
                patch("utils.integrations.hero_sms._hero_sms_prices_v3_by_service", return_value=[
                    {"country": 16, "providers": []},
                    {"country": 50, "providers": []},
                ]), \
                patch("utils.integrations.hero_sms._hero_sms_country_is_on_cooldown", return_value=False):
            candidates = hero_sms._hero_sms_build_number_candidates(
                proxies=None,
                service_code="dr",
                preferred_country=50,
            )

        self.assertEqual(
            [
                {"country": 16, "provider_ids": [2442, 3237]},
                {"country": 50, "provider_ids": [2266, 2442]},
            ],
            candidates,
        )

    def test_reuse_claim_prefers_global_pool_when_current_country_has_none(self):
        hero_sms._HERO_SMS_REUSE_STATE["items"] = [
            {
                "activation_id": "aid-global",
                "phone": "+551100001111",
                "service": "dr",
                "country": 10,
                "provider_ids": [2920, 3042],
                "uses": 0,
                "created_at": 9999999999.0,
                "updated_at": 9999999999.0,
                "in_use": False,
                "claimed_by": "",
                "last_result": "success",
            }
        ]
        aid, phone, uses = hero_sms._hero_sms_reuse_claim("dr", 6, [2920])
        self.assertEqual("aid-global", aid)
        self.assertEqual("+551100001111", phone)
        self.assertEqual(0, uses)

    def test_multi_candidates_keep_trying_after_no_balance(self):
        attempted = []

        def fake_get_number(proxies, *, service_code="", country_id=None, provider_id=None, provider_ids=None):
            attempted.append((int(country_id), list(provider_ids or [])))
            if len(attempted) == 1:
                return "", "", "NO_BALANCE"
            return "", "", "NO_NUMBERS"

        with patch("utils.integrations.hero_sms._hero_sms_enabled", return_value=True), \
                patch("utils.integrations.hero_sms._hero_sms_max_tries", return_value=2), \
                patch("utils.integrations.hero_sms.hero_sms_get_balance", return_value=(0.01, "")), \
                patch("utils.integrations.hero_sms._hero_sms_update_runtime"), \
                patch("utils.integrations.hero_sms._hero_sms_resolve_service_code", return_value="dr"), \
                patch("utils.integrations.hero_sms._hero_sms_resolve_country_id", return_value=33), \
                patch("utils.integrations.hero_sms._hero_sms_multi_selector_enabled", return_value=True), \
                patch("utils.integrations.hero_sms._hero_sms_build_number_candidates", return_value=[
                    {"country": 33, "provider_ids": [3243, 3237]},
                    {"country": 6, "provider_ids": [3061]},
                ]), \
                patch("utils.integrations.hero_sms._hero_sms_reuse_enabled", return_value=False), \
                patch("utils.integrations.hero_sms._hero_sms_get_number", side_effect=fake_get_number), \
                patch("utils.integrations.hero_sms._sleep_interruptible", return_value=False):
            with redirect_stdout(io.StringIO()):
                ok, reason = hero_sms._try_verify_phone_via_hero_sms(
                    session=object(),
                    proxies={"http": "http://proxy", "https": "http://proxy"},
                )

        self.assertFalse(ok)
        self.assertEqual("取号失败: NO_NUMBERS", reason)
        self.assertEqual([(33, [3243, 3237]), (33, [3243, 3237]), (6, [3061]), (6, [3061])], attempted)

    def test_poll_code_reads_nested_json_code_without_status_ok(self):
        responses = [
            (True, '{"status":"SUCCESS","data":{"sms":{"code":"654321"}}}', {
                "status": "SUCCESS",
                "data": {"sms": {"code": "654321"}},
            }),
        ]

        with patch("utils.integrations.hero_sms._hero_sms_request", side_effect=responses), \
                patch("utils.integrations.hero_sms._hero_sms_poll_timeout_sec", return_value=30), \
                patch("utils.integrations.hero_sms._sleep_interruptible", return_value=False):
            with redirect_stdout(io.StringIO()):
                code = hero_sms._hero_sms_poll_code("123", proxies=None)

        self.assertEqual("654321", code)

    def test_same_candidate_buys_new_number_again_after_timeout(self):
        get_number_calls = []

        def fake_get_number(proxies, *, service_code="", country_id=None, provider_id=None, provider_ids=None):
            get_number_calls.append((int(country_id), list(provider_ids or [])))
            return f"aid-{len(get_number_calls)}", f"+1000000000{len(get_number_calls)}", ""

        with ExitStack() as stack:
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_enabled", return_value=True))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_max_tries", return_value=3))
            stack.enter_context(patch("utils.integrations.hero_sms.hero_sms_get_balance", return_value=(5.0, "")))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_update_runtime"))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_resolve_service_code", return_value="dr"))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_resolve_country_id", return_value=6))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_multi_selector_enabled", return_value=True))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_build_number_candidates", return_value=[
                {"country": 6, "provider_ids": [3061, 3138]},
            ]))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_reuse_enabled", return_value=True))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_reuse_claim", return_value=("", "", 0)))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_get_number", side_effect=fake_get_number))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_country_record_result"))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_set_status"))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_country_mark_success"))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_mark_ready"))
            stack.enter_context(patch("utils.integrations.hero_sms._build_sentinel_for_session", return_value=""))
            stack.enter_context(patch("utils.integrations.hero_sms._post_with_retry", return_value=types.SimpleNamespace(status_code=200, json=lambda: {}, text="")))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_poll_code", return_value=""))
            stack.enter_context(patch("utils.integrations.hero_sms._sleep_interruptible", return_value=False))
            stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_country_label", return_value="印度尼西亚(6)"))
            reuse_add_mock = stack.enter_context(patch("utils.integrations.hero_sms._hero_sms_reuse_add"))
            with redirect_stdout(io.StringIO()):
                ok, reason = hero_sms._try_verify_phone_via_hero_sms(
                    session=object(),
                    proxies={"http": "http://proxy", "https": "http://proxy"},
                )

        self.assertFalse(ok)
        self.assertEqual("接码超时，未收到手机验证码", reason)
        self.assertEqual([(6, [3061, 3138]), (6, [3061, 3138]), (6, [3061, 3138])], get_number_calls)
        reuse_add_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

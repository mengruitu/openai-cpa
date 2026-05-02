from unittest.mock import patch

from utils.integrations.smsbower_sms import _smsbower_prices_by_service, _smsbower_clean_id_csv


def test_smsbower_clean_id_csv_deduplicates_and_ignores_invalid_values():
    assert _smsbower_clean_id_csv(" 12,abc,12，34, 0 ") == "12,34,0"


def test_smsbower_prices_flatten_provider_inventory_and_filter_provider_ids():
    prices_payload = {
        "69": {
            "dr": {
                "101": {"count": 4, "price": 0.07, "provider_id": 101},
                "102": {"count": 2, "price": 0.05, "provider_id": 102},
                "103": {"count": 0, "price": 0.04, "provider_id": 103},
            }
        },
        "50": {
            "dr": {
                "201": {"count": 5, "price": 0.08, "provider_id": 201},
            }
        },
    }

    def fake_request(action, *, proxies, params=None, timeout=25):
        if action == "getCountries":
            return True, "", {
                "69": {"id": 69, "eng": "United States"},
                "50": {"id": 50, "eng": "Netherlands"},
            }
        if action == "getPrices":
            return True, "", prices_payload
        raise AssertionError(action)

    with patch("utils.integrations.smsbower_sms._smsbower_request", side_effect=fake_request):
        rows = _smsbower_prices_by_service(
            "dr",
            proxies=None,
            force_refresh=True,
            provider_ids="101,201",
            except_provider_ids="",
        )

    assert [row["provider_id"] for row in rows] == [101, 201]
    assert rows[0]["country"] == 69
    assert rows[0]["cost"] == 0.07
    assert rows[0]["count"] == 4


def test_smsbower_prices_can_exclude_provider_ids():
    prices_payload = {
        "69": {
            "dr": {
                "101": {"count": 4, "price": 0.07, "provider_id": 101},
                "102": {"count": 2, "price": 0.05, "provider_id": 102},
            }
        },
    }

    def fake_request(action, *, proxies, params=None, timeout=25):
        if action == "getCountries":
            return True, "", {"69": {"id": 69, "eng": "United States"}}
        if action == "getPrices":
            return True, "", prices_payload
        raise AssertionError(action)

    with patch("utils.integrations.smsbower_sms._smsbower_request", side_effect=fake_request):
        rows = _smsbower_prices_by_service(
            "dr",
            proxies=None,
            force_refresh=True,
            provider_ids="",
            except_provider_ids="102",
        )

    assert [row["provider_id"] for row in rows] == [101]

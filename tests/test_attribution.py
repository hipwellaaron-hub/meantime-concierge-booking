import datetime as dt

from app.services import attribution


# --- classify_referrer -------------------------------------------------


def test_classify_referrer_unknown_when_blank():
    assert attribution.classify_referrer(None) == "unknown"
    assert attribution.classify_referrer("") == "unknown"


def test_classify_referrer_search_engine():
    assert attribution.classify_referrer("https://www.google.com/search?q=function+venue") == "search"
    assert attribution.classify_referrer("https://www.bing.com/search?q=x") == "search"


def test_classify_referrer_social():
    assert attribution.classify_referrer("https://www.facebook.com/") == "social"
    assert attribution.classify_referrer("https://l.instagram.com/") == "social"


def test_classify_referrer_other_site_is_referral():
    assert attribution.classify_referrer("https://some-wedding-blog.example/best-venues") == "referral"


def test_classify_referrer_unparseable_url_is_referral_not_a_crash():
    # urlparse raises ValueError on a malformed IPv6-shaped host -- a
    # real, reachable case, not a hypothetical.
    assert attribution.classify_referrer("http://[invalid") == "referral"


def test_classify_referrer_lenient_garbage_url_is_unknown_not_a_crash():
    # urlparse is lenient with most garbage -- this doesn't raise, it
    # just yields no host, which correctly falls through to "unknown".
    assert attribution.classify_referrer("not a url at all::::") == "unknown"


# --- build_touch ---------------------------------------------------------


def test_build_touch_captures_utm_and_click_ids():
    bundle = attribution.build_touch({
        "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "spring-promo",
        "utm_term": "function venue", "utm_content": "ad1",
        "gclid": "Cj0KCQjw", "fbclid": None, "referrer": "https://www.google.com/",
    })
    assert bundle["utm_source"] == "google"
    assert bundle["utm_medium"] == "cpc"
    assert bundle["utm_campaign"] == "spring-promo"
    assert bundle["gclid"] == "Cj0KCQjw"
    assert bundle["fbclid"] is None
    assert bundle["referrer_category"] == "search"


def test_build_touch_with_only_referrer_classifies_it():
    bundle = attribution.build_touch({"referrer": "https://www.facebook.com/somepage"})
    assert bundle["referrer_category"] == "social"
    assert all(bundle[f] is None for f in attribution.UTM_FIELDS + attribution.CLICK_ID_FIELDS)


def test_build_touch_with_nothing_at_all_is_unknown_not_direct():
    bundle = attribution.build_touch({})
    assert bundle["referrer_category"] == "unknown"
    assert bundle["referrer"] is None


def test_build_touch_caps_field_length():
    huge = "x" * 10_000
    bundle = attribution.build_touch({"utm_campaign": huge})
    assert len(bundle["utm_campaign"]) == attribution.MAX_FIELD_LENGTH


def test_build_touch_ignores_non_string_values():
    bundle = attribution.build_touch({"utm_source": 12345, "gclid": ["not", "a", "string"]})
    assert bundle["utm_source"] is None
    assert bundle["gclid"] is None


def test_build_touch_preserves_client_captured_at():
    bundle = attribution.build_touch({"captured_at": "2026-08-01T09:00:00.000Z"})
    assert bundle["captured_at"] == "2026-08-01T09:00:00.000Z"


def test_build_touch_stamps_captured_at_when_missing():
    bundle = attribution.build_touch({})
    parsed = dt.datetime.fromisoformat(bundle["captured_at"])
    assert parsed.tzinfo is not None


# --- parse_attribution_payload ---------------------------------------------


def test_parse_attribution_payload_valid_json():
    raw = '{"first_touch": {"utm_source": "google", "gclid": "abc123"}, "last_touch": {"utm_source": "direct-remarketing"}}'
    first, last = attribution.parse_attribution_payload(raw)
    assert first["utm_source"] == "google"
    assert first["gclid"] == "abc123"
    assert last["utm_source"] == "direct-remarketing"


def test_parse_attribution_payload_malformed_json_returns_none_without_fallback():
    first, last = attribution.parse_attribution_payload("{not valid json")
    assert first is None
    assert last is None


def test_parse_attribution_payload_missing_returns_none_without_fallback():
    first, last = attribution.parse_attribution_payload(None)
    assert first is None
    assert last is None


def test_parse_attribution_payload_falls_back_to_server_referrer():
    first, last = attribution.parse_attribution_payload(None, fallback_referrer="https://www.google.com/")
    assert first == last
    assert first["referrer_category"] == "search"


def test_parse_attribution_payload_malformed_json_still_uses_fallback():
    first, last = attribution.parse_attribution_payload("garbage{{{", fallback_referrer="https://www.facebook.com/")
    assert first["referrer_category"] == "social"


def test_parse_attribution_payload_not_a_dict_falls_back():
    first, last = attribution.parse_attribution_payload("[1, 2, 3]", fallback_referrer=None)
    assert first is None
    assert last is None


# --- summarize_channel -----------------------------------------------------


def test_summarize_channel_none_bundle_is_unknown():
    assert attribution.summarize_channel(None) == "Unknown"


def test_summarize_channel_gclid_beats_everything():
    bundle = attribution.build_touch({"gclid": "abc", "utm_source": "facebook", "utm_medium": "social"})
    assert attribution.summarize_channel(bundle) == "Google Ads (paid)"


def test_summarize_channel_fbclid():
    bundle = attribution.build_touch({"fbclid": "xyz"})
    assert attribution.summarize_channel(bundle) == "Meta Ads (paid)"


def test_summarize_channel_paid_medium():
    bundle = attribution.build_touch({"utm_source": "bing", "utm_medium": "cpc"})
    assert attribution.summarize_channel(bundle) == "Paid (bing)"


def test_summarize_channel_campaign_with_source():
    bundle = attribution.build_touch({"utm_source": "newsletter", "utm_medium": "email"})
    assert attribution.summarize_channel(bundle) == "Campaign (newsletter)"


def test_summarize_channel_organic_search():
    bundle = attribution.build_touch({"referrer": "https://www.google.com/"})
    assert attribution.summarize_channel(bundle) == "Organic search"


def test_summarize_channel_organic_social():
    bundle = attribution.build_touch({"referrer": "https://www.instagram.com/"})
    assert attribution.summarize_channel(bundle) == "Organic social"


def test_summarize_channel_referral():
    bundle = attribution.build_touch({"referrer": "https://some-blog.example/"})
    assert attribution.summarize_channel(bundle) == "Referral"


def test_summarize_channel_unknown_bundle_present_but_empty():
    bundle = attribution.build_touch({})
    assert attribution.summarize_channel(bundle) == "Unknown"


# --- current_quarter_start --------------------------------------------------


def test_current_quarter_start_q1():
    assert attribution.current_quarter_start(dt.date(2026, 2, 15)) == dt.date(2026, 1, 1)


def test_current_quarter_start_q3():
    assert attribution.current_quarter_start(dt.date(2026, 8, 14)) == dt.date(2026, 7, 1)


def test_current_quarter_start_q4_boundary():
    assert attribution.current_quarter_start(dt.date(2026, 10, 1)) == dt.date(2026, 10, 1)

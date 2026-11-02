"""What the app (WP-13) relies on from GET /api/config: the update prompt and the privacy / terms links.

The app reads it on every start, logged in or not, and sends its saved login with it; an old or broken login
must not turn into a 401 (which logs the person out). min_app_version is compared part by part as numbers
("1.10.0" > "1.2.0", frontend/src/app-config.ts); by default it must not ask 1.1.0 or 1.2.0 phones to update.
"""
import re


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def test_config_is_public_even_with_a_bad_login(client):
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
        r = client.get("/api/config", headers=headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json()["features"], dict)


def test_default_min_version_keeps_current_apps_working(client):
    config = client.get("/api/config").json()
    v = config["min_app_version"]
    assert re.fullmatch(r"\d+(\.\d+)*", v), v
    assert _version(v) <= _version("1.1.0")  # QA APK 1.1.0 and the 1.2.0 build are never told to update
    for optional in ("privacy_url", "terms_url", "update_url"):  # when the server sends them, only https
        assert optional not in config or str(config[optional]).startswith("https://")

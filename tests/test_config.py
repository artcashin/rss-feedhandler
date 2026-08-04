import pytest
from pathlib import Path
from rss_ticker.config import load_config, ConfigError


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_full_config(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://nas.local:8088
admin_key: ${{TICKER_ADMIN_KEY}}
manifest_key: mk
retention_days: 3
default_poll_interval_s: 120
max_concurrent_polls: 4
users:
  - id: art
    name: Art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", name: A}}
      - {{url: "https://b.example/rss", poll_interval_s: 600}}
    filters:
      - {{pattern: nvidia, action: highlight}}
""")
    cfg = load_config(p, {"TICKER_ADMIN_KEY": "s3cret"})
    assert cfg.public_base_url == "http://nas.local:8088"
    assert cfg.admin_key == "s3cret"
    assert cfg.retention_days == 3
    assert cfg.max_concurrent_polls == 4
    assert len(cfg.users) == 1
    u = cfg.users[0]
    assert u.id == "art"
    assert u.feeds[0].name == "A"
    assert u.feeds[0].poll_interval_s is None
    assert u.feeds[1].poll_interval_s == 600
    assert u.filters[0].action == "highlight"


def test_feed_group_is_parsed(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", name: A, group: Markets}}
""")
    cfg = load_config(p, {})
    assert cfg.users[0].feeds[0].group == "Markets"


def test_feed_without_group_is_none(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", name: A}}
""")
    cfg = load_config(p, {})
    assert cfg.users[0].feeds[0].group is None


def test_defaults_applied(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\n")
    cfg = load_config(p, {})
    assert cfg.retention_days == 7
    assert cfg.default_poll_interval_s == 300
    assert cfg.max_concurrent_polls == 8
    assert cfg.users == ()


def test_missing_public_base_url_is_error(tmp_path):
    p = write(tmp_path, "admin_key: k\nmanifest_key: mk\n")
    with pytest.raises(ConfigError, match="public_base_url"):
        load_config(p, {})


def test_unset_env_var_is_error(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: ${NOPE}\nmanifest_key: mk\n")
    with pytest.raises(ConfigError, match="NOPE"):
        load_config(p, {})


def test_bad_filter_action_is_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    filters:
      - {{pattern: p, action: banish}}
""")
    with pytest.raises(ConfigError, match="banish"):
        load_config(p, {})


def test_duplicate_user_id_is_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - {{id: art, token: "{TOKEN}"}}
  - {{id: art, token: "rotated-{TOKEN}"}}
""")
    with pytest.raises(ConfigError, match="duplicate user id"):
        load_config(p, {})


def test_user_id_with_space_is_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - {id: "art bob"}
""")
    with pytest.raises(ConfigError, match="letters, digits, hyphens"):
        load_config(p, {})


def test_user_id_with_ampersand_is_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - {id: "art&admin=1"}
""")
    with pytest.raises(ConfigError, match="letters, digits, hyphens"):
        load_config(p, {})


def test_user_id_with_hash_is_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - {id: "art#frag"}
""")
    with pytest.raises(ConfigError, match="letters, digits, hyphens"):
        load_config(p, {})


def test_plain_slug_user_id_is_accepted(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - {{id: "art_1-b", token: "{TOKEN}"}}
""")
    cfg = load_config(p, {})
    assert cfg.users[0].id == "art_1-b"


TOKEN = "tkn-" + "0123456789abcdef" * 3


def test_manifest_key_is_loaded_and_env_expanded(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: ${TICKER_MANIFEST_KEY}
""")
    cfg = load_config(p, {"TICKER_MANIFEST_KEY": "mk"})
    assert cfg.manifest_key == "mk"


def test_missing_manifest_key_is_error(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\n")
    with pytest.raises(ConfigError, match="manifest_key"):
        load_config(p, {})


def test_manifest_key_must_differ_from_admin_key(tmp_path):
    # manifest_key is pasted into a third-party UI. If it is the admin key,
    # handing OpenBB a read credential also hands it write access.
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: k\n")
    with pytest.raises(ConfigError, match="must differ"):
        load_config(p, {})


def test_user_token_is_loaded_and_env_expanded(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: ${TICKER_TOKEN_ART}
""")
    cfg = load_config(p, {"TICKER_TOKEN_ART": TOKEN})
    assert cfg.users[0].token == TOKEN


def test_user_without_a_token_is_a_startup_error(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})


def test_token_error_does_not_leak_the_value(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: shortsecret
  - id: bob
    token: {TOKEN}
""")
    with pytest.raises(ConfigError) as exc:
        load_config(p, {})
    assert "shortsecret" not in str(exc.value)
    assert "art" in str(exc.value)


def test_short_token_is_an_error(tmp_path):
    # A token arriving through an env var can be a placeholder or a truncated
    # paste; nothing else would catch it.
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: abc
""")
    with pytest.raises(ConfigError, match="at least 32"):
        load_config(p, {})


def test_duplicate_token_across_users_is_an_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
  - id: bob
    token: {TOKEN}
""")
    with pytest.raises(ConfigError, match="duplicate token"):
        load_config(p, {})


def test_zero_max_concurrent_polls_is_an_error(tmp_path):
    # 0 becomes asyncio.Semaphore(0) in the poller, which blocks forever --
    # the poller is silently dead with no feed ever polled.
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\nmax_concurrent_polls: 0\n")
    with pytest.raises(ConfigError, match="max_concurrent_polls"):
        load_config(p, {})


def test_negative_retention_days_is_an_error(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\nretention_days: -1\n")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(p, {})


def test_non_numeric_retention_days_is_a_config_error(tmp_path):
    # Must raise ConfigError, not a bare ValueError, like every other
    # validation path in this file (M1 from an earlier review).
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\nretention_days: soon\n")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(p, {})


def test_valid_max_concurrent_polls_is_applied(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\nmax_concurrent_polls: 4\n")
    cfg = load_config(p, {})
    assert cfg.max_concurrent_polls == 4


def test_feed_title_format_is_parsed(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", title_format: "{{title}} - {{author}}"}}
""")
    cfg = load_config(p, {})
    assert cfg.users[0].feeds[0].title_format == "{title} - {author}"


def test_feed_without_title_format_is_none(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss"}}
""")
    cfg = load_config(p, {})
    assert cfg.users[0].feeds[0].title_format is None


def test_feed_title_format_must_be_a_string(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", title_format: 3}}
""")
    with pytest.raises(ConfigError, match="title_format"):
        load_config(p, {})


def test_feed_poll_interval_must_be_at_least_one(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", poll_interval_s: -5}}
""")
    with pytest.raises(ConfigError, match="poll_interval_s"):
        load_config(p, {})


def test_feed_poll_interval_zero_is_rejected(tmp_path):
    p = write(tmp_path, f"""
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
    token: {TOKEN}
    feeds:
      - {{url: "https://a.example/rss", poll_interval_s: 0}}
""")
    with pytest.raises(ConfigError, match="poll_interval_s"):
        load_config(p, {})


def test_missing_config_file_raises_friendly_error_not_traceback(tmp_path):
    # The single most common first-run mistake (mounting the container wrong)
    # should name the path and point at the mount, not dump a FileNotFoundError
    # traceback.
    missing = tmp_path / "config" / "config.yaml"
    with pytest.raises(ConfigError) as ei:
        load_config(missing, {})
    msg = str(ei.value)
    assert str(missing) in msg
    assert "mount" in msg.lower()
    assert "not found" in msg.lower()


def test_config_path_that_is_a_directory_is_reported_as_such(tmp_path):
    # A bind mount to a host path that doesn't exist creates a DIRECTORY at
    # the mount point; read_text then raises IsADirectoryError. Name that
    # specific footgun rather than leaking the raw errno.
    d = tmp_path / "config.yaml"
    d.mkdir()
    with pytest.raises(ConfigError, match="directory"):
        load_config(d, {})


LOGIN = "you@github"


def test_tailscale_auth_defaults_off(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\nmanifest_key: mk\n")
    cfg = load_config(p, {})
    assert cfg.tailscale_auth is False
    assert cfg.bind_host == "0.0.0.0"


def test_tailscale_auth_requires_a_loopback_bind(tmp_path):
    # The Tailscale-User-Login header is only trustworthy while Serve is the
    # sole way in. Bound to 0.0.0.0 inside the sidecar's netns the port is
    # reachable by every tailnet peer, around Serve, where the header is
    # trivially forged. Refuse to start rather than serve a forgeable door.
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 0.0.0.0
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    with pytest.raises(ConfigError, match="loopback"):
        load_config(p, {})


def test_tailscale_auth_accepts_loopback(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    cfg = load_config(p, {})
    assert cfg.tailscale_auth is True
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.users[0].tailscale_login == LOGIN
    assert cfg.users[0].token == ""


def test_manifest_key_optional_under_tailscale_auth(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
""")
    assert load_config(p, {}).manifest_key == ""


def test_manifest_key_still_required_without_tailscale_auth(tmp_path):
    p = write(tmp_path, "public_base_url: http://x\nadmin_key: k\n")
    with pytest.raises(ConfigError, match="manifest_key"):
        load_config(p, {})


def test_user_with_neither_token_nor_login_is_an_error(tmp_path):
    # A user nothing can authenticate as is a config mistake, not a silently
    # closed account.
    p = write(tmp_path, """
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})


def test_duplicate_tailscale_login_is_an_error(tmp_path):
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
  - id: bob
    tailscale_login: {LOGIN}
""")
    with pytest.raises(ConfigError, match="duplicate tailscale_login"):
        load_config(p, {})


def test_two_tokenless_users_are_not_a_duplicate_token_error(tmp_path):
    # Both tokens are "", which must not trip the duplicate-token check.
    p = write(tmp_path, f"""
public_base_url: https://x
admin_key: k
tailscale_auth: true
bind_host: 127.0.0.1
users:
  - id: art
    tailscale_login: {LOGIN}
  - id: bob
    tailscale_login: bob@github
""")
    assert [u.id for u in load_config(p, {}).users] == ["art", "bob"]


def test_token_still_required_without_tailscale_auth(tmp_path):
    p = write(tmp_path, """
public_base_url: http://x
admin_key: k
manifest_key: mk
users:
  - id: art
""")
    with pytest.raises(ConfigError, match="art"):
        load_config(p, {})

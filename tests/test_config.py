from pathlib import Path

import pytest

from rss_ticker.config import ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_the_four_operational_keys(tmp_path):
    p = write(tmp_path, """
retention_days: 3
default_poll_interval_s: 120
max_concurrent_polls: 4
bind_host: 127.0.0.1
""")
    cfg = load_config(p, {})
    assert (cfg.retention_days, cfg.default_poll_interval_s, cfg.max_concurrent_polls) == (3, 120, 4)
    assert cfg.bind_host == "127.0.0.1"


def test_defaults_apply_to_an_empty_file(tmp_path):
    cfg = load_config(write(tmp_path, ""), {})
    assert (cfg.retention_days, cfg.default_poll_interval_s, cfg.max_concurrent_polls) == (7, 300, 8)
    assert cfg.bind_host == "0.0.0.0"


def test_env_expansion_still_works(tmp_path):
    cfg = load_config(write(tmp_path, "default_poll_interval_s: ${POLL}\n"), {"POLL": "90"})
    assert cfg.default_poll_interval_s == 90


def test_unset_env_variable_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="POLL"):
        load_config(write(tmp_path, "default_poll_interval_s: ${POLL}\n"), {})


def test_a_v8_config_fails_naming_every_stale_key(tmp_path):
    p = write(tmp_path, """
public_base_url: https://t.example
admin_key: k
manifest_key: mk
tailscale_auth: true
retention_days: 7
users:
  - id: art
""")
    with pytest.raises(ConfigError) as exc:
        load_config(p, {})
    message = str(exc.value)
    for key in ("admin_key", "manifest_key", "public_base_url", "tailscale_auth", "users"):
        assert key in message
    assert "retention_days" not in message.split("Keep only")[0]


@pytest.mark.parametrize("key", ["retention_days", "default_poll_interval_s", "max_concurrent_polls"])
def test_non_positive_and_non_numeric_values_are_errors(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, f"{key}: 0\n"), {})
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, f"{key}: soon\n"), {})


def test_missing_file_and_directory_are_named_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", {})
    with pytest.raises(ConfigError, match="directory"):
        load_config(tmp_path, {})


def test_root_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- just\n- a list\n"), {})

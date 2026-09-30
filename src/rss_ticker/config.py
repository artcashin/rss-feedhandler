from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# A channel key is a URL-safe slug (it becomes a query-param value and an
# OpenBB option value). A handle is a YouTube @handle: the leading @ then the
# characters YouTube allows in a handle. Both are validated at load so a
# malformed config fails at boot -- the same loud-failure contract the four
# operational keys already have -- rather than surfacing as a 404 or a
# mis-built /@<handle>/live URL at request time.
_KEY_RE = re.compile(r"[a-z0-9-]+")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    handle: str

# The whole vocabulary. Anything else is an error, so a config from the
# user-and-key era (users:, the admin and manifest keys, the public base url,
# ...) fails loudly at boot instead of being silently ignored into an empty
# pool.
KNOWN_KEYS = frozenset(
    {"retention_days", "default_poll_interval_s", "max_concurrent_polls", "bind_host", "live_tv"}
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    bind_host: str = "0.0.0.0"
    live_tv: tuple[Channel, ...] = ()


def _expand(value, env: Mapping[str, str]):
    if not isinstance(value, str):
        return value

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in env:
            raise ConfigError(f"config references unset environment variable {name}")
        return env[name]

    return _ENV_RE.sub(sub, value)


def _walk(node, env: Mapping[str, str]):
    if isinstance(node, dict):
        return {k: _walk(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, env) for v in node]
    return _expand(node, env)


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
    if value < 1:
        raise ConfigError(f"{key} must be at least 1, got {value!r}")
    return value


_CHANNEL_FIELDS = ("key", "label", "handle")


def _channels(raw: dict) -> tuple[Channel, ...]:
    """Parse and validate the optional live_tv list.

    Absent or empty means the feature is off, so this returns (). Every other
    shape is checked and any problem raises ConfigError, naming the offending
    value -- validating here (not at request time) means a bad handle can never
    become a fetch against a mis-built URL, and a duplicate key can never
    silently shadow an earlier channel.
    """
    value = raw.get("live_tv")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"live_tv must be a list of channels, got {type(value).__name__}")
    channels: list[Channel] = []
    seen: set[str] = set()
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"live_tv[{i}] must be a mapping with key, label, handle")
        unknown = sorted(str(k) for k in item if k not in _CHANNEL_FIELDS)
        if unknown:
            raise ConfigError(f"live_tv[{i}] has unknown fields: {', '.join(unknown)}")
        for field in _CHANNEL_FIELDS:
            if not isinstance(item.get(field), str) or not item[field]:
                raise ConfigError(f"live_tv[{i}] {field} must be a non-empty string")
        key, label, handle = item["key"], item["label"], item["handle"]
        if not _KEY_RE.fullmatch(key):
            raise ConfigError(f"live_tv[{i}] key {key!r} must match [a-z0-9-]+")
        if not _HANDLE_RE.fullmatch(handle):
            raise ConfigError(f"live_tv[{i}] handle {handle!r} must look like @name")
        if key in seen:
            raise ConfigError(f"live_tv has a duplicate key: {key}")
        seen.add(key)
        channels.append(Channel(key=key, label=label, handle=handle))
    # A tuple, not the list: Config is frozen and compares by value, and the
    # field is typed tuple[Channel, ...].
    return tuple(channels)


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        # The most common first-run mistake: the container has no config
        # mounted. Say so in one line instead of dumping a traceback.
        raise ConfigError(
            f"config file not found at {path} -- did you mount it into the "
            f"container? Mount your config.yaml at CONFIG_PATH (default "
            f"/config/config.yaml); see the README deployment section."
        ) from None
    except IsADirectoryError:
        # A bind mount pointed at a host path that did not exist creates a
        # directory at the mount point -- a distinct, confusing footgun.
        raise ConfigError(
            f"config path {path} is a directory, not a file -- a bind mount to "
            f"a host path that doesn't exist creates a directory there. Create "
            f"the config.yaml file on the host first, then mount it (or its "
            f"folder)."
        ) from None
    except OSError as exc:
        raise ConfigError(f"could not read config at {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    unknown = sorted(str(k) for k in raw if k not in KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f"config has keys this version does not use: {', '.join(unknown)}. "
            f"This server has no users, keys or configured feeds -- clients "
            f"subscribe feeds over the websocket. Keep only: "
            f"{', '.join(sorted(KNOWN_KEYS))}."
        )

    raw = _walk(raw, env)

    bind_host = raw.get("bind_host", "0.0.0.0")
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("bind_host must be a non-empty string")

    return Config(
        retention_days=_positive_int(raw, "retention_days", 7),
        default_poll_interval_s=_positive_int(raw, "default_poll_interval_s", 300),
        max_concurrent_polls=_positive_int(raw, "max_concurrent_polls", 8),
        bind_host=bind_host,
        live_tv=_channels(raw),
    )

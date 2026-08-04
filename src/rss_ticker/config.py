from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ACTIONS = ("include", "highlight")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MIN_TOKEN_LEN = 32

# Hosts on which the Tailscale-User-Login header is trustworthy, because only
# a proxy on this machine (Tailscale Serve) can reach the port.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FeedConfig:
    url: str
    name: str | None = None
    poll_interval_s: int | None = None
    group: str | None = None
    title_format: str | None = None


@dataclass(frozen=True)
class FilterConfig:
    pattern: str
    action: str


@dataclass(frozen=True)
class UserConfig:
    id: str
    name: str | None = None
    feeds: tuple[FeedConfig, ...] = ()
    filters: tuple[FilterConfig, ...] = ()
    token: str = ""
    tailscale_login: str = ""


@dataclass(frozen=True)
class Config:
    public_base_url: str
    admin_key: str
    manifest_key: str = ""
    retention_days: int = 7
    default_poll_interval_s: int = 300
    max_concurrent_polls: int = 8
    users: tuple[UserConfig, ...] = ()
    tailscale_auth: bool = False
    bind_host: str = "0.0.0.0"


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


def _feed(raw: dict) -> FeedConfig:
    url = raw.get("url")
    if not url:
        raise ConfigError("every feed needs a url")
    title_format = raw.get("title_format")
    if title_format is not None and not isinstance(title_format, str):
        raise ConfigError(
            f"title_format for feed {url!r} must be a string "
            'like "{title} - {author}", got ' + repr(title_format)
        )
    poll_interval_s = raw.get("poll_interval_s")
    if poll_interval_s is not None:
        # A non-positive interval puts next_poll_at in the past on every
        # schedule, and base_interval * 2**n stays negative through the
        # backoff cap -- a silent once-per-tick hammer against a third-party
        # server that backoff can never slow down. Reject it at startup.
        try:
            poll_interval_s = int(poll_interval_s)
        except (TypeError, ValueError):
            raise ConfigError(
                f"poll_interval_s for feed {url!r} must be a whole number, "
                f"got {poll_interval_s!r}"
            ) from None
        if poll_interval_s < 1:
            raise ConfigError(
                f"poll_interval_s for feed {url!r} must be at least 1, "
                f"got {poll_interval_s!r}"
            )
    return FeedConfig(
        url=url,
        name=raw.get("name"),
        poll_interval_s=poll_interval_s,
        group=raw.get("group"),
        title_format=title_format,
    )


def _filter(raw: dict) -> FilterConfig:
    pattern = raw.get("pattern")
    action = raw.get("action")
    if not pattern:
        raise ConfigError("every filter needs a pattern")
    if action not in _ACTIONS:
        raise ConfigError(f"filter action {action!r} must be one of {_ACTIONS}")
    return FilterConfig(pattern=pattern, action=action)


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
    if value < 1:
        raise ConfigError(f"{key} must be at least 1, got {value!r}")
    return value


def _user(raw: dict, tailscale_auth: bool = False) -> UserConfig:
    uid = raw.get("id")
    if not uid:
        raise ConfigError("every user needs an id")
    if not _USER_ID_RE.match(uid):
        raise ConfigError(
            f"user id {uid!r} must contain only letters, digits, hyphens and underscores"
        )

    login = raw.get("tailscale_login") or ""
    if login and not isinstance(login, str):
        raise ConfigError(f"tailscale_login for user {uid!r} must be a string")

    token = raw.get("token")
    if not token:
        if not tailscale_auth:
            raise ConfigError(
                f"user {uid!r} has no token; generate one with "
                "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if not login:
            raise ConfigError(
                f"user {uid!r} has neither a token nor a tailscale_login, so nothing "
                f"can authenticate as them"
            )
        token = ""
    elif not isinstance(token, str) or len(token) < MIN_TOKEN_LEN:
        raise ConfigError(
            f"token for user {uid!r} must be a string of at least {MIN_TOKEN_LEN} characters"
        )

    return UserConfig(
        id=uid,
        name=raw.get("name"),
        feeds=tuple(_feed(f) for f in raw.get("feeds") or []),
        filters=tuple(_filter(f) for f in raw.get("filters") or []),
        token=token,
        tailscale_login=login,
    )


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

    raw = _walk(raw, env)

    if not raw.get("public_base_url"):
        raise ConfigError("public_base_url is required")
    if not raw.get("admin_key"):
        raise ConfigError("admin_key is required")

    tailscale_auth = bool(raw.get("tailscale_auth", False))
    bind_host = raw.get("bind_host", "0.0.0.0")
    if not isinstance(bind_host, str) or not bind_host:
        raise ConfigError("bind_host must be a non-empty string")
    if tailscale_auth and bind_host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"tailscale_auth requires a loopback bind_host (one of "
            f"{', '.join(sorted(LOOPBACK_HOSTS))}), got {bind_host!r}; the "
            f"Tailscale-User-Login header is only trustworthy when Tailscale "
            f"Serve is the only way to reach this server"
        )

    manifest_key = raw.get("manifest_key") or ""
    if not manifest_key and not tailscale_auth:
        raise ConfigError(
            "manifest_key is required; it gates widgets.json and is the value pasted "
            "into OpenBB Workspace, so it must not be the admin key"
        )
    if manifest_key and manifest_key == raw["admin_key"]:
        raise ConfigError("manifest_key must differ from admin_key")

    users = tuple(_user(u, tailscale_auth) for u in raw.get("users") or [])
    seen: set[str] = set()
    seen_tokens: set[str] = set()
    seen_logins: set[str] = set()
    for u in users:
        if u.id in seen:
            raise ConfigError(f"duplicate user id {u.id!r}")
        seen.add(u.id)
        # Empty tokens are legitimate under tailscale_auth; only real ones can collide.
        if u.token and u.token in seen_tokens:
            raise ConfigError(f"duplicate token configured for user {u.id!r}")
        if u.token:
            seen_tokens.add(u.token)
        if u.tailscale_login and u.tailscale_login in seen_logins:
            raise ConfigError(f"duplicate tailscale_login configured for user {u.id!r}")
        if u.tailscale_login:
            seen_logins.add(u.tailscale_login)

    return Config(
        public_base_url=raw["public_base_url"].rstrip("/"),
        admin_key=raw["admin_key"],
        manifest_key=manifest_key,
        retention_days=_positive_int(raw, "retention_days", 7),
        default_poll_interval_s=_positive_int(raw, "default_poll_interval_s", 300),
        max_concurrent_polls=_positive_int(raw, "max_concurrent_polls", 8),
        users=users,
        tailscale_auth=tailscale_auth,
        bind_host=bind_host,
    )

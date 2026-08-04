from __future__ import annotations

import logging

from .config import Config
from .fetch import redact_feed_url
from .store import Store

log = logging.getLogger(__name__)


def reconcile(store: Store, config: Config, now: int) -> None:
    """Additively apply config to the database. Never deletes data.

    A user's row, feeds, subscriptions, and articles are never removed here.
    The one thing that is withdrawn is a credential: a user no longer present
    in config.yaml has their token revoked below, closing every read they
    previously had -- see revoke_tokens_except for why that is safe even
    against a config with no users at all.
    """
    for user in config.users:
        store.upsert_user(user.id, user.name, now=now, token=user.token or None)
        # upsert_user's COALESCE preserves a previously stored token when
        # config passes None, which is right for an *omitted* token line
        # under normal reconciliation -- but under tailscale_auth, a user
        # configured with no token has switched to identity auth and config
        # is authoritative: the retired token (the one that was published in
        # widgets.json and proxy logs) must stop working, not survive by
        # COALESCE. Clear it explicitly rather than relying on preservation.
        if config.tailscale_auth and not user.token:
            store.clear_token(user.id)
        for feed in user.feeds:
            feed_id = store.upsert_feed(
                feed.url,
                name=feed.name,
                poll_interval_s=feed.poll_interval_s,
                now=now,
                group=feed.group,
                title_format=feed.title_format,
            )
            store.subscribe(user.id, feed_id)
        for rule in user.filters:
            store.add_filter(user.id, rule.pattern, rule.action)
        log.info(
            "Reconciled user %s with %d feeds and %d filters",
            user.id,
            len(user.feeds),
            len(user.filters),
        )

    # Additive reconciliation never unsubscribes: a feed removed from
    # config.yaml keeps its subscription and stays polled forever. That is
    # deliberate (config removal must not destroy data), but it must be
    # VISIBLE -- otherwise a removed feed silently burns polls for months.
    # Names the feed and its redacted URL (feed URLs routinely carry
    # credentials); the admin closes it out with DELETE /api/feeds/<id>.
    configured_urls = {feed.url for user in config.users for feed in user.feeds}
    orphan_feeds = [
        f for f in store.all_feeds() if f.enabled and f.url not in configured_urls
    ]
    if orphan_feeds:
        log.warning(
            "Feeds enabled in the database but absent from config "
            "(still polled; remove with DELETE /api/feeds/<id>): %s",
            ", ".join(
                f"{f.name or '?'} (id {f.id}, {redact_feed_url(f.url)})"
                for f in orphan_feeds
            ),
        )

    # Computed before revocation so a user revoked by the call below is not
    # also restated here in the same breath -- the two warnings describe
    # different events (never had a credential vs. just lost one) and a user
    # transitioning should read as one, not both at once.
    orphans = store.users_without_tokens()

    # Under tailscale_auth a configured tailscale_login authenticates by
    # identity, not by token -- token IS NULL is the expected, working state
    # for that user, not a broken one. Excluding them here is what keeps a
    # correctly-working tailnet deployment quiet on every boot; a user with
    # neither a token nor a login is still worth warning about.
    identity_ids = (
        {u.id for u in config.users if u.tailscale_login}
        if config.tailscale_auth
        else set()
    )
    orphans = [o for o in orphans if o not in identity_ids]

    revoked = store.revoke_tokens_except({user.id for user in config.users})
    if revoked:
        log.warning(
            "Revoked tokens for users no longer in config: %s",
            ", ".join(revoked),
        )

    if orphans:
        log.warning(
            "Users in the database with no token cannot authenticate: %s",
            ", ".join(orphans),
        )

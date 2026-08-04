from __future__ import annotations

from urllib.parse import quote

from .config import Config

# Floor Workspace enforces on the resize handle. A headline row is ~31px, so
# dragged much below its min height the list is all scrollbar and no content.
MIN_WIDTH = 12

# prefix, label, height, min height, description. The rail is deliberately
# short, so its floor is its natural height rather than the window's.
SIZES = (
    ("news_window", "News window", 8, 4, "Live RSS headlines, newest first, with scrollback"),
    ("news_rail", "News rail", 2, 2, "Live RSS headlines in a compact bottom rail"),
)


def render_widgets(config: Config) -> dict:
    manifest: dict[str, dict] = {}
    for user in config.users:
        # subCategory's documented purpose is refining search, so it carries
        # what the widget actually contains: the feed groups that become its
        # bottom tabs. Searching "Substack" in the picker then finds this
        # ticker. Distinct, in first-seen (config) order.
        groups: list[str] = []
        for feed in user.feeds:
            if feed.group and feed.group not in groups:
                groups.append(feed.group)

        for prefix, label, height, min_height, description in SIZES:
            widget: dict = {
                "name": f"{label} ({user.name or user.id})",
                "description": description,
                "category": "News",
                "type": "iframe",
                "endpoint": (
                    f"{config.public_base_url}/widget"
                    f"?user={quote(user.id, safe='')}"
                    # Under tailscale_auth the caller is identified by the
                    # header Serve injects, so publishing a token here would
                    # put a credential in the iframe URL for nothing.
                    + (
                        ""
                        if config.tailscale_auth
                        else f"&token={quote(user.token, safe='')}"
                    )
                ),
                "gridData": {
                    "w": 40,
                    "h": height,
                    "minW": MIN_WIDTH,
                    "minH": min_height,
                },
                "source": "RSS",
            }
            # Omitted rather than sent empty: a blank subCategory is noise in
            # the picker, and a feed list with no groups has nothing to refine.
            if groups:
                widget["subCategory"] = ", ".join(groups)
            manifest[f"{prefix}_{user.id}"] = widget
    return manifest

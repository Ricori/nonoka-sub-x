"""Where the share commands point when nobody says otherwise.

Resolution order, narrowest first:

1. ``--remote`` on the command line;
2. ``[share] remote`` in ``config.toml`` (for anyone running their own
   deployment, or pointing at a mirror);
3. the built-in mailbox below.

The point of (3) is that a contributor should be able to install the package
and run ``share pull`` — no address to be told, no file to write.
"""

from __future__ import annotations

import base64

from finesub import settings

#: The built-in address, stored XOR-folded rather than in the clear.
#:
#: ⚠ **This is not a security measure and must not be described as one.**
#: Whatever the runtime can decode, so can anyone holding the package — and the
#: address is a public entry point by design; it is printed in error messages,
#: it is on the wire, and contributors are meant to have it. The one thing it
#: does is keep the host out of the reach of scrapers that grep public
#: repositories for ``workers.dev`` addresses, which is precisely and only what
#: it was asked to do (owner, 2026-09-09).
#:
#: Abuse is bounded by the deployment instead: anonymous registrations are
#: capped, so are pending pushes per contributor and in total, everything
#: expires after 30 days, and nothing enters the corpus without the
#: maintainer's verdict. Rate limiting at the Cloudflare edge is the next lever
#: if it is ever needed — none of that depends on the address being hard to find.
_PACKED_MAILBOX = "AxZZFhxWSwJUWVxTGBdPSxwEBV9XHkFGHhBZBQ4eCANFX0BdDhBeSAsJEg=="
#: Deliberately unrelated to the address, though only one half of it actually
#: carries entropy: the Worker's name is public anyway (`wrangler.jsonc`, and
#: the deploy README's example host), so what a scraper cannot get from the
#: repository is the ACCOUNT SUBDOMAIN. The first fold key was the Worker's own
#: name, which gave away the half that is free rather than the half that is
#: not — the test greps this file for both regardless, since a key that leaks
#: any part of the host is a key chosen without thinking (reviewer, 2026-09-09).
_FOLD_KEY = b"kb-fold-2026"


def _unfold(packed: str) -> str:
    raw = base64.b64decode(packed.encode("ascii"))
    return bytes(byte ^ _FOLD_KEY[i % len(_FOLD_KEY)] for i, byte in enumerate(raw)).decode("utf-8")


def builtin_mailbox() -> str:
    """The packaged default, with no config lookup — mostly for tests."""

    return _unfold(_PACKED_MAILBOX)


def default_remote() -> str:
    """The address to use when the command line did not name one."""

    # Blank counts as unset here: the built-in address is computed, so it cannot
    # be the declaration's default.
    configured = settings.SHARE_REMOTE.configured()
    if configured is not None and configured.strip():
        return configured.strip()
    return builtin_mailbox()


#: The public half of the maintainer's snapshot signing key
#: (``share keygen``), for the built-in mailbox. Unlike the address this is
#: meant to be public; it is what lets a stolen mailbox token or a hijacked
#: Worker publish nothing a client will accept. Generated 2026-09-25; the
#: private half stays on the maintainer's machine. Changing it means every
#: installed client refuses the library until it upgrades -- a rotation ships
#: as a release, and the maintainer says so.
BUILTIN_SIGNING_KEY = "HYiqpbgO2blaVoYw003Hpwa1SL1QWlKHEi5lzrme978"


def trusted_signing_key(url: str) -> str:
    """The key a pull from ``url`` must be signed by, when the client knows
    one in advance: the built-in key for the built-in address, ``[share]
    signing_key`` for any other (it names the user's own library -- applying
    it to the built-in one would refuse that library forever). "" means
    trust the library's first signed snapshot."""

    if url.rstrip("/") == builtin_mailbox().rstrip("/"):
        return BUILTIN_SIGNING_KEY
    configured = settings.SHARE_SIGNING_KEY.configured()
    if configured is not None and configured.strip():
        return configured.strip()
    return ""

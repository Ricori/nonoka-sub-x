"""Snapshot signatures: who published this, not only "the server said so".

The hash chain proves a snapshot extends what the server said before; it does
not prove the maintainer said it. Anyone who can write the mailbox's
``/publish`` (its token, or the Cloudflare account) could extend the chain with
anything, and every client would accept it. So the maintainer's machine signs
the chain head with an Ed25519 key that never leaves it, and clients verify
against a public key they already hold (``sync.check_signature``).

What is signed is the head ``(corpus_id, server_rev, chain_hash)``: the chain
hash commits to the content digest and the whole history, and
``verify_snapshot`` checks the content against it, so signing the head signs
the snapshot.

Keys travel as unpadded base64url of the raw 32 bytes -- one line, easy to
paste into ``config.toml`` or the source.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Mapping

from .exchange import ExchangeError

ALGORITHM = "ed25519"
_CONTEXT = "finesub-share-snapshot/1"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _ed25519():  # type: ignore[no-untyped-def]
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - declared in [harness]
        raise ExchangeError(
            "snapshot signatures need the `cryptography` package (pip install -e \".[harness]\")"
        ) from exc
    return ed25519


def generate_key_file(path: str | Path) -> str:
    """Write a new private key to ``path`` (refusing to overwrite one) and
    return its public key."""

    target = Path(path).expanduser()
    if target.exists():
        raise ExchangeError(f"{target} already exists; a signing key is never overwritten")
    seed = os.urandom(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL: two keygens racing on one path must not both think they won.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(_b64encode(seed) + "\n")
    return public_key_of(load_private_key(target))


def load_private_key(path: str | Path) -> bytes:
    seed = _b64decode(Path(path).expanduser().read_text(encoding="ascii").strip())
    if len(seed) != 32:
        raise ExchangeError(f"{path} is not an Ed25519 signing key")
    return seed


def public_key_of(seed: bytes) -> str:
    key = _ed25519().Ed25519PrivateKey.from_private_bytes(seed)
    return _b64encode(key.public_key().public_bytes_raw())


def _message(snapshot: Mapping[str, Any]) -> bytes:
    return "\n".join(
        (
            _CONTEXT,
            str(snapshot.get("corpus_id") or ""),
            str(int(snapshot.get("server_rev") or 0)),
            str(snapshot.get("chain_hash") or ""),
        )
    ).encode("utf-8")


def sign_snapshot(snapshot: Mapping[str, Any], seed: bytes) -> dict[str, str]:
    """The ``signature`` object a published snapshot carries."""

    key = _ed25519().Ed25519PrivateKey.from_private_bytes(seed)
    return {
        "alg": ALGORITHM,
        "public_key": public_key_of(seed),
        "value": _b64encode(key.sign(_message(snapshot))),
    }


def verify_signature(snapshot: Mapping[str, Any], public_key: str) -> None:
    """Raise unless ``snapshot`` carries a valid signature by ``public_key``."""

    signature = snapshot.get("signature")
    if not isinstance(signature, Mapping):
        raise ExchangeError("snapshot is not signed")
    if signature.get("alg") != ALGORITHM:
        raise ExchangeError(f"unsupported snapshot signature algorithm {signature.get('alg')!r}")
    if signature.get("public_key") != public_key:
        raise ExchangeError("snapshot is signed by a different key than the one trusted for this library")
    ed25519 = _ed25519()
    from cryptography.exceptions import InvalidSignature

    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(_b64decode(public_key))
        key.verify(_b64decode(str(signature.get("value") or "")), _message(snapshot))
    except (InvalidSignature, ValueError) as exc:
        raise ExchangeError("snapshot signature does not verify") from exc

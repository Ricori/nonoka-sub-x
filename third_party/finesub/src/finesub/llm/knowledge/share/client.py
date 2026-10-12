"""HTTP client half (urllib, stdlib-only) for the share protocol."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


class RemoteError(RuntimeError):
    pass


class NotModified(RemoteError):
    """The server answered 304 to a conditional request.

    A ``RemoteError`` because that is what an unprepared caller's contract says
    it may see; :func:`fetch_snapshot`, the only one that asks for it, turns it
    into :data:`UNCHANGED`.
    """


#: A full snapshot of a large corpus is megabytes; 64 MB is generous headroom
#: and still a bound — the admission boundary cannot protect a client that a
#: hostile server OOMs before admission runs (round 14).
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: Every request says who it is. Not politeness: urllib's default
#: ``Python-urllib/3.x`` is blocked outright by Cloudflare's edge protections,
#: which answered every single call to a workers.dev deployment with a bare
#: ``error code: 1010`` and no JSON — a failure with no path back to the cause
#: (found on the first live deployment, 2026-09-09). Any honest identifier
#: passes; hiding behind a browser string would be both a lie and no more
#: effective.
#:
#: Deliberately bare: a contact URL here would ship the deployer's identity to
#: every server the client touches, and this string's only job is to not be
#: urllib's default.
USER_AGENT = "finesub-share/1"

#: What ``fetch_snapshot`` returns when the server says "you already have it".
UNCHANGED = object()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib follows a 30x by copying our headers -- contributor and
    maintainer tokens included -- to whatever host the Location names. No share
    endpoint redirects, so one is refused rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise RemoteError(
            f"{req.full_url} redirected ({code}) to {newurl}; point --remote at the final address"
        )


_OPENER = urllib.request.build_opener(_NoRedirects())


def _urlopen(request: urllib.request.Request, *, timeout: float):  # type: ignore[no-untyped-def]
    return _OPENER.open(request, timeout=timeout)


def _require_tls(url: str) -> None:
    """Tokens travel in headers, so a remote other than this machine must be
    https: over http anyone on the path reads them, and can rewrite what a
    pull receives before the signature is even looked at."""

    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (parts.hostname or "") in {"127.0.0.1", "localhost", "::1"}:
        return
    raise RemoteError(f"{url}: a share remote must be https:// (plain http only on localhost)")


def _request(
    url: str,
    *,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    _require_tls(url)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RemoteError(f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # urllib treats everything outside 2xx as an error, 304 included. Only
        # the caller that sent a conditional header knows one is good news, so
        # it surfaces as a RemoteError subclass: whoever asked for it catches
        # it, and anyone who did not still gets the error their contract
        # promises rather than a sentinel object they will call `.get()` on.
        if exc.code == 304:
            raise NotModified(f"304 {url}: not modified") from exc
        try:
            detail = json.loads(exc.read(65536).decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        raise RemoteError(f"{exc.code} {url}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RemoteError(f"cannot reach {url}: {exc.reason}") from exc


def register(base_url: str) -> str:
    return str(_request(f"{base_url.rstrip('/')}/register", method="POST")["token"])


def fetch_identity(base_url: str) -> dict[str, Any]:
    return _request(f"{base_url.rstrip('/')}/identity")


def fetch_snapshot(base_url: str, *, known_chain_hash: str = "") -> Any:
    """The corpus, or :data:`UNCHANGED` when our copy is already current.

    The chain head is a strong ETag — it commits to the entire content — and
    the client already stores it as the anti-rollback anchor, so asking "still
    this one?" costs nothing to remember. A pull that has nothing to do is by
    far the common case, and this is what makes it cheap for both sides.
    """

    if not known_chain_hash:
        return _request(f"{base_url.rstrip('/')}/snapshot")
    try:
        return _request(
            f"{base_url.rstrip('/')}/snapshot",
            headers={"If-None-Match": f'"{known_chain_hash}"'},
        )
    except NotModified:
        return UNCHANGED


def push_bundle(base_url: str, bundle: Mapping[str, Any], *, token: str) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/push",
        method="POST",
        body=bundle,
        headers={"X-Share-Token": token},
    )


def push_status(base_url: str, queue_id: int, *, token: str) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/push/{int(queue_id)}", headers={"X-Share-Token": token}
    )


# ---- mailbox endpoints (the relay's transport, plan B) --------------------
#
# Only the public deployment has these. They move queue items and snapshots
# between the mailbox and the maintainer's machine, which is where every
# judgement still happens.


def fetch_inbox(
    base_url: str, *, maintainer_token: str, after: int = 0, limit: int = 20
) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/inbox?after={int(after)}&limit={int(limit)}",
        headers={"X-Maintainer-Token": maintainer_token},
    )


def fetch_tracked(
    base_url: str, *, maintainer_token: str, after: int = 0, limit: int = 50
) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/tracked?after={int(after)}&limit={int(limit)}",
        headers={"X-Maintainer-Token": maintainer_token},
    )


def post_outbox(
    base_url: str, results: list[Mapping[str, Any]], *, maintainer_token: str
) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/outbox",
        method="POST",
        body={"results": list(results)},
        headers={"X-Maintainer-Token": maintainer_token},
    )


def publish_snapshot(
    base_url: str, snapshot: Mapping[str, Any], *, maintainer_token: str
) -> dict[str, Any]:
    """The snapshot travels as the request body and its identity as the query
    string, so the mailbox never has to parse what it is serving."""

    query = urllib.parse.urlencode(
        {
            "corpus_id": str(snapshot.get("corpus_id") or ""),
            "server_rev": int(snapshot.get("server_rev") or 0),
            "chain_hash": str(snapshot.get("chain_hash") or ""),
        }
    )
    return _request(
        f"{base_url.rstrip('/')}/publish?{query}",
        method="POST",
        body=snapshot,
        headers={"X-Maintainer-Token": maintainer_token},
    )


# ---- maintainer endpoints (the review CLI's transport) --------------------


def lease_queue(
    base_url: str,
    *,
    maintainer_token: str,
    seconds: int = 900,
    limit: int = 10,
    queue_id: int | None = None,
) -> dict[str, Any]:
    query = f"lease_seconds={int(seconds)}&limit={int(limit)}"
    if queue_id is not None:
        query += f"&queue_id={int(queue_id)}"
    return _request(
        f"{base_url.rstrip('/')}/queue?{query}",
        headers={"X-Maintainer-Token": maintainer_token},
    )


def peek_queue(base_url: str, *, maintainer_token: str, queue_id: int | None = None) -> dict[str, Any]:
    query = f"?queue_id={int(queue_id)}" if queue_id is not None else ""
    return _request(
        f"{base_url.rstrip('/')}/queue/peek{query}",
        headers={"X-Maintainer-Token": maintainer_token},
    )


def release_item(base_url: str, *, maintainer_token: str, queue_id: int, lease_token: str) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/release",
        method="POST",
        body={"queue_id": queue_id, "lease_token": lease_token},
        headers={"X-Maintainer-Token": maintainer_token},
    )


def post_verdict(
    base_url: str,
    *,
    maintainer_token: str,
    queue_id: int,
    lease_token: str,
    expected_version: int,
    verdict: str,
    reason: str = "",
    merge: Mapping[str, str] | None = None,
    evidence: list[Mapping[str, Any]] | None = None,
    override: str = "",
) -> dict[str, Any]:
    return _request(
        f"{base_url.rstrip('/')}/verdict",
        method="POST",
        body={
            "queue_id": queue_id,
            "lease_token": lease_token,
            "expected_version": expected_version,
            "verdict": verdict,
            "reason": reason,
            "merge": dict(merge or {}),
            "evidence": list(evidence or []),
            "override": override,
        },
        headers={"X-Maintainer-Token": maintainer_token},
    )

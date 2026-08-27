"""
gwauth.py — keystore-backed authentication for the AI Gateway (v2 cutover piece).

This is the ACTIVATION half of step 2c. keystore.py is storage-only; this module
is the auth logic that main.py's check_api_key delegates to. It is a separate,
importable module for exactly the reason the other v2 modules are: so the
highest-risk change in the project — the one on the auth hot path, where a bug
is a total lockout — can be proven in isolation against fakes before it is wired
into the live gateway.

WHAT IT DOES
    - Extracts the bearer token, hashes it (via keystore), looks it up.
    - Returns the full key record: client, allowed_classes, weight, capability.
    - Derives a v1 `tier` int from `weight` so every existing call site
      (acquire_slot(model, client, tier), get_tier_config(tier), the dashboards)
      keeps working UNCHANGED. tier is a pure backward-compat shim now: in the
      current config all three tiers share the same node pool and differ only in
      timeout, so weight -> tier is a lossless-enough mapping and weight remains
      the real ordering concept in the v2 dispatcher.

WEIGHT -> TIER
    critical -> 1, high -> 1, normal -> 2, low -> 3.
    critical+high both map to tier 1 (the 60s-timeout, most-headroom tier);
    since tiers no longer steer nodes, mapping the two top weights to the most
    generous timeout is the safe direction.

EMPTY-KEYSTORE SAFETY NET (Option 3)
    A hard swap onto an empty keys.db would lock out ALL traffic, including live
    /api/chat, with no way in. So: if the keystore has zero rows, this module
    does NOT hard-fail. It logs CRITICAL once, and for each request falls back to
    the OLD env/config lookup (injected as `legacy_lookup`), logging a WARNING
    per request so the fallback can never run silently. The moment the keystore
    has >=1 row, the legacy path is never consulted again — that is the cutover.

STRICT PAIRING
    endpoint_class_ok lives in keystore.py. Handlers that know the declared class
    (/api/chat => 'interactive'; /api/jobs => class from body) call
    enforce_endpoint_class() here to apply it. Authentication (who are you) and
    authorization (may you use this class on this transport) are two steps: this
    module does both, but they are separate calls so the sync path can auth
    without a class in hand if ever needed.
"""

import logging

import keystore

log = logging.getLogger("gateway")

# weight -> v1 tier int. See module docstring for rationale.
_WEIGHT_TO_TIER = {"critical": 1, "high": 1, "normal": 2, "low": 3}
_DEFAULT_TIER = 2  # matches the old get_key_info default


def weight_to_tier(weight: str) -> int:
    return _WEIGHT_TO_TIER.get(weight, _DEFAULT_TIER)


class AuthError(Exception):
    """Raised for a missing/invalid key. main.py maps this to the right HTTP code.

    status is the HTTP status the gateway should return (401 missing, 403 invalid),
    matching the old check_api_key behavior exactly.
    """
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# Module state, injected by init() from main.py.
_store: keystore.KeyStore = None
_require_api_key = True
_legacy_lookup = None            # callable(token_str) -> {"name","tier"} | None
_last_known_empty = None         # None=not yet observed; else store's emptiness on the last check


def init(store, require_api_key=True, legacy_lookup=None):
    """
    store          : a keystore.KeyStore instance (already opened on keys.db)
    require_api_key : the gateway's REQUIRE_API_KEY flag
    legacy_lookup  : the OLD get_key_info(token) -> {"name","tier"}|None, used
                     ONLY as the empty-keystore safety net.
    """
    global _store, _require_api_key, _legacy_lookup, _last_known_empty
    _store = store
    _require_api_key = require_api_key
    _legacy_lookup = legacy_lookup
    _last_known_empty = None


def _extract_bearer(headers) -> str | None:
    """Return the raw token from an Authorization: Bearer <t> header, or None."""
    auth = headers.get("Authorization", "") or ""
    if auth.startswith("Bearer "):
        tok = auth[len("Bearer "):].strip()
        return tok or None
    return None


def authenticate(headers) -> dict:
    """
    Core auth. Takes something with .get('Authorization') (a request's headers).
    Returns a record dict:
        {client, allowed_classes, weight, capability, tier, _source}
    where _source is 'keystore' or 'legacy' (for logging/tests).

    Raises AuthError(401) on a missing token, AuthError(403) on an invalid one,
    mirroring the old check_api_key status codes. When REQUIRE_API_KEY is False
    and no/invalid key is given, returns the anonymous record (tier 2), exactly
    as the old function did.
    """
    global _last_known_empty
    token = _extract_bearer(headers)

    # --- Empty-keystore safety net -----------------------------------------
    # If the store is empty we must NOT lock everyone out. Fall back to the old
    # env/config lookup, loudly, until keys exist. Logs CRITICAL on the first
    # empty check ever AND on every populated -> empty transition (e.g. the
    # last key getting revoked), not just once per process lifetime -- an
    # admin emptying the store months into the cutover needs the same loud
    # signal as the original pre-cutover state, since it has the same effect:
    # every client silently falls back to legacy auth again.
    store_count = _store.count() if _store is not None else 0
    if store_count == 0:
        if not _last_known_empty:
            log.critical(
                "AUTH: keystore is EMPTY — falling back to legacy env/config keys. "
                "This is the pre-cutover safety net; create keystore keys to activate v2 auth.")
        _last_known_empty = True
        return _legacy_authenticate(token)
    _last_known_empty = False

    # --- Normal path: keystore is the single source ------------------------
    if token is None:
        if _require_api_key:
            raise AuthError(401, "Missing API key")
        return _anon()

    rec = _store.lookup(token)
    if rec is None:
        if _require_api_key:
            raise AuthError(403, "Invalid API key")
        return _anon()

    return {
        "client": rec["client"],
        "allowed_classes": rec.get("allowed_classes") or (),
        "weight": rec.get("weight", "normal"),
        "capability": rec.get("capability"),
        "tier": weight_to_tier(rec.get("weight", "normal")),
        "_source": "keystore",
    }


def _legacy_authenticate(token) -> dict:
    """Old env/config lookup, used only while the keystore is empty."""
    if token is None:
        if _require_api_key:
            raise AuthError(401, "Missing API key")
        return _anon()
    info = _legacy_lookup(token) if _legacy_lookup else None
    if info is None:
        if _require_api_key:
            raise AuthError(403, "Invalid API key")
        return _anon()
    log.warning(f"AUTH: request served via LEGACY key fallback (client={info['name']})")
    tier = info.get("tier", _DEFAULT_TIER)
    return {
        "client": info["name"],
        "allowed_classes": (),          # legacy keys have no class model
        "weight": "normal",
        "capability": None,
        "tier": tier,
        "_source": "legacy",
    }


def _anon() -> dict:
    return {
        "client": "anonymous",
        "allowed_classes": (),
        "weight": "normal",
        "capability": None,
        "tier": _DEFAULT_TIER,
        "_source": "anonymous",
    }


def enforce_endpoint_class(record: dict, requested_class: str, is_async_endpoint: bool):
    """
    Apply strict endpoint/class pairing. Call from a handler that knows the
    declared class. Raises AuthError(403) on refusal. No-op for legacy/anonymous
    records (empty allowed_classes) so the safety net path is not broken by
    class checks it can't satisfy.
    """
    if record.get("_source") in ("legacy", "anonymous"):
        return
    ok, reason = keystore.endpoint_class_ok(record, requested_class, is_async_endpoint)
    if not ok:
        raise AuthError(403, f"class check failed: {reason}")

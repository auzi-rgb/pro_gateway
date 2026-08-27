"""
test_gwauth.py — exercise gwauth.py (the keystore-backed auth layer) in isolation.

Run: python3 test_gwauth.py

Covers the things that make this the highest-risk change:
  - correct record shape and weight->tier mapping for real keystore keys
  - missing vs invalid token -> the SAME status codes as the old function (401/403)
  - REQUIRE_API_KEY False -> anonymous, never raises
  - strict endpoint/class pairing enforced (right class/transport pass, others fail)
  - THE SAFETY NET: empty keystore falls back to legacy lookup and NEVER locks out;
    a populated keystore NEVER consults legacy even if legacy keys still exist
  - revoked key stops authenticating immediately
  - malformed Authorization headers don't 500
"""

import os
import tempfile

import keystore
import gwauth

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   - {label}")
    else:
        FAIL += 1
        print(f"  FAIL - {label}")


class H:
    """Minimal stand-in for request.headers: a dict with .get()."""
    def __init__(self, auth=None):
        self._d = {}
        if auth is not None:
            self._d["Authorization"] = auth
    def get(self, k, default=""):
        return self._d.get(k, default)


def bearer(tok):
    return H(f"Bearer {tok}")


# A fake legacy lookup standing in for main.py's get_key_info.
def make_legacy(mapping):
    def _lookup(token):
        return mapping.get(token)
    return _lookup


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "keys_test.db")
    ks = keystore.KeyStore(db_path=db)

    # Legacy keys that still "exist" in env/config — the safety net should use
    # these ONLY while the keystore is empty, and NEVER once it is populated.
    legacy = make_legacy({
        "LEGACY-OWUI": {"name": "openwebui-legacy", "tier": 2},
        "LEGACY-HD":   {"name": "hiredesk-legacy", "tier": 1},
    })

    # ---------------------------------------------------------------------
    print("\n[1] SAFETY NET: empty keystore falls back to legacy, never locks out")
    gwauth.init(ks, require_api_key=True, legacy_lookup=legacy)
    check("keystore starts empty", ks.count() == 0)
    rec = gwauth.authenticate(bearer("LEGACY-OWUI"))
    check("legacy key authenticates via fallback", rec["client"] == "openwebui-legacy")
    check("fallback record marked _source=legacy", rec["_source"] == "legacy")
    check("legacy tier preserved", rec["tier"] == 2)
    # An unknown key while empty: still rejected (fallback isn't a free pass).
    try:
        gwauth.authenticate(bearer("NOPE"))
        check("unknown key still rejected during fallback", False)
    except gwauth.AuthError as e:
        check("unknown key still rejected during fallback (403)", e.status == 403)

    # ---------------------------------------------------------------------
    print("\n[2] populate keystore -> legacy path is abandoned (THE CUTOVER)")
    s_owui = ks.create("openwebui", ["interactive"], "normal")
    s_hd = ks.create("hiredesk", ["interactive", "throughput"], "normal", capability="high")
    gwauth.init(ks, require_api_key=True, legacy_lookup=legacy)  # re-init, count now 2
    check("keystore now populated", ks.count() == 2)
    # The SAME legacy token that worked in [1] must now fail — keystore is sole source.
    try:
        gwauth.authenticate(bearer("LEGACY-OWUI"))
        check("legacy key NO LONGER works once keystore populated", False)
    except gwauth.AuthError as e:
        check("legacy key NO LONGER works once keystore populated (403)", e.status == 403)

    # ---------------------------------------------------------------------
    print("\n[3] real keystore keys authenticate with correct shape")
    rec = gwauth.authenticate(bearer(s_owui))
    check("openwebui authenticates", rec["client"] == "openwebui")
    check("source is keystore", rec["_source"] == "keystore")
    check("allowed_classes present", rec["allowed_classes"] == ("interactive",))
    check("weight present", rec["weight"] == "normal")
    check("normal weight -> tier 2", rec["tier"] == 2)
    hd = gwauth.authenticate(bearer(s_hd))
    check("hiredesk multi-class present",
          set(hd["allowed_classes"]) == {"interactive", "throughput"})
    check("hiredesk capability high", hd["capability"] == "high")

    # ---------------------------------------------------------------------
    print("\n[4] weight -> tier mapping")
    s_crit = ks.create("crit-app", ["throughput"], "critical")
    s_high = ks.create("high-app", ["throughput"], "high")
    s_low = ks.create("low-app", ["throughput"], "low")
    check("critical -> tier 1", gwauth.authenticate(bearer(s_crit))["tier"] == 1)
    check("high -> tier 1", gwauth.authenticate(bearer(s_high))["tier"] == 1)
    check("low -> tier 3", gwauth.authenticate(bearer(s_low))["tier"] == 3)

    # ---------------------------------------------------------------------
    print("\n[5] missing vs invalid -> same codes as the old function")
    try:
        gwauth.authenticate(H())  # no Authorization header at all
        check("missing key raises", False)
    except gwauth.AuthError as e:
        check("missing key -> 401", e.status == 401)
    try:
        gwauth.authenticate(bearer("garbage-token"))
        check("invalid key raises", False)
    except gwauth.AuthError as e:
        check("invalid key -> 403", e.status == 403)

    # ---------------------------------------------------------------------
    print("\n[6] malformed Authorization headers don't explode")
    for bad in ["", "Bearer", "Bearer    ", "Basic xyz", "bearer lowercase"]:
        try:
            gwauth.authenticate(H(bad))
            check(f"malformed header {bad!r} handled (no key -> 401 expected path)", False)
        except gwauth.AuthError as e:
            check(f"malformed header {bad!r} -> clean 401/403 (got {e.status})",
                  e.status in (401, 403))
        except Exception as e:
            check(f"malformed header {bad!r} did NOT raise a non-AuthError ({type(e).__name__})", False)

    # ---------------------------------------------------------------------
    print("\n[7] REQUIRE_API_KEY False -> anonymous, never raises")
    gwauth.init(ks, require_api_key=False, legacy_lookup=legacy)
    rec = gwauth.authenticate(H())            # no key
    check("no key -> anonymous when not required", rec["client"] == "anonymous")
    check("anonymous tier 2", rec["tier"] == 2)
    rec = gwauth.authenticate(bearer("garbage"))  # bad key
    check("bad key -> anonymous when not required", rec["client"] == "anonymous")
    gwauth.init(ks, require_api_key=True, legacy_lookup=legacy)  # restore

    # ---------------------------------------------------------------------
    print("\n[8] strict endpoint/class pairing via enforce_endpoint_class")
    hd = gwauth.authenticate(bearer(s_hd))  # {interactive, throughput}
    # interactive on sync -> ok
    gwauth.enforce_endpoint_class(hd, "interactive", is_async_endpoint=False)
    check("hiredesk interactive on /api/chat passes", True)
    # throughput on async -> ok
    gwauth.enforce_endpoint_class(hd, "throughput", is_async_endpoint=True)
    check("hiredesk throughput on /api/jobs passes", True)
    # interactive on async -> wrong transport
    try:
        gwauth.enforce_endpoint_class(hd, "interactive", is_async_endpoint=True)
        check("interactive on async refused", False)
    except gwauth.AuthError as e:
        check("interactive on async refused (403)", e.status == 403)
    # deadline (not in set) -> refused
    try:
        gwauth.enforce_endpoint_class(hd, "deadline", is_async_endpoint=True)
        check("ungranted class refused", False)
    except gwauth.AuthError as e:
        check("ungranted class refused (403)", e.status == 403)
    # openwebui (interactive-only) doing throughput -> refused
    owui = gwauth.authenticate(bearer(s_owui))
    try:
        gwauth.enforce_endpoint_class(owui, "throughput", is_async_endpoint=True)
        check("interactive-only key refused throughput", False)
    except gwauth.AuthError as e:
        check("interactive-only key refused throughput (403)", e.status == 403)

    # ---------------------------------------------------------------------
    print("\n[9] revoke stops authentication immediately")
    check("revoke crit-app", ks.revoke("crit-app") is True)
    try:
        gwauth.authenticate(bearer(s_crit))
        check("revoked key no longer authenticates", False)
    except gwauth.AuthError as e:
        check("revoked key -> 403", e.status == 403)

    # ---------------------------------------------------------------------
    print("\n[10] enforce is a no-op for legacy/anonymous (safety net not broken)")
    # Drain the store to force the empty-fallback path, then confirm a legacy
    # record is not blocked by a class check it structurally can't satisfy.
    for c in ["openwebui", "hiredesk", "high-app", "low-app"]:
        ks.revoke(c)
    check("store drained to empty", ks.count() == 0)
    gwauth.init(ks, require_api_key=True, legacy_lookup=legacy)
    legrec = gwauth.authenticate(bearer("LEGACY-HD"))
    check("legacy record returned during empty fallback", legrec["_source"] == "legacy")
    gwauth.enforce_endpoint_class(legrec, "interactive", is_async_endpoint=False)  # must not raise
    check("enforce no-ops on legacy record", True)

    # ---------------------------------------------------------------------
    print("\n[11] CRITICAL alert re-arms on a same-session populated -> empty transition")
    # Bug #10 (audit): revoking the last key mid-session must re-trigger the
    # loud empty-keystore signal, not just once ever at process start. Proven
    # via the internal _last_known_empty latch, which is what gates the
    # log.critical() call -- same test intent without needing a log capture.
    ks2 = keystore.KeyStore(db_path=os.path.join(tmp, "keys_test2.db"))
    gwauth.init(ks2, require_api_key=True, legacy_lookup=legacy)
    check("fresh init: not yet observed", gwauth._last_known_empty is None)
    gwauth.authenticate(bearer("LEGACY-OWUI"))
    check("first check on empty store sets the latch", gwauth._last_known_empty is True)
    s_tmp = ks2.create("temp-client", ["throughput"], "normal")
    gwauth.authenticate(bearer(s_tmp))
    check("populating the store clears the latch", gwauth._last_known_empty is False)
    ks2.revoke("temp-client")
    check("store empty again (no re-init in between)", ks2.count() == 0)
    gwauth.authenticate(bearer("LEGACY-OWUI"))
    check("SAME-SESSION transition back to empty re-arms the latch (bug #10 fix)",
          gwauth._last_known_empty is True)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

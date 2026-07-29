"""
test_keystore.py — exercise keystore.py (allowed_classes model) against a temp DB.

Run: python3 test_keystore.py
"""

import os
import tempfile

from keystore import (
    KeyStore, hash_key, class_allowed, endpoint_class_ok, _normalize_classes,
    VALID_CLASSES, VALID_WEIGHTS,
)

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


def main():
    tmp = tempfile.mkdtemp()
    ks = KeyStore(db_path=os.path.join(tmp, "keys_test.db"))

    print("\n[1] create with a single-class allowed set")
    s_owui = ks.create("openwebui", ["interactive"], "normal")
    rec = ks.lookup(s_owui)
    check("create returns a secret", isinstance(s_owui, str) and len(s_owui) > 30)
    check("lookup finds it", rec is not None)
    check("allowed_classes is a tuple", isinstance(rec["allowed_classes"], tuple))
    check("single class stored", rec["allowed_classes"] == ("interactive",))
    check("hash never exposed", "key_hash" not in rec)

    print("\n[2] HireDesk: multi-class key (the case that drove this design)")
    s_hd = ks.create("hiredesk", ["interactive", "throughput"], "normal",
                     capability="high")
    hd = ks.lookup(s_hd)
    check("hiredesk allows both classes",
          set(hd["allowed_classes"]) == {"interactive", "throughput"})
    check("stored sorted/deduped",
          hd["allowed_classes"] == ("interactive", "throughput"))
    check("capability is high (24b) for everything", hd["capability"] == "high")
    check("single weight regardless of classes", hd["weight"] == "normal")

    print("\n[3] class_allowed membership")
    check("hiredesk may do interactive", class_allowed(hd, "interactive"))
    check("hiredesk may do throughput", class_allowed(hd, "throughput"))
    check("hiredesk may NOT do deadline", not class_allowed(hd, "deadline"))
    check("openwebui may NOT do throughput",
          not class_allowed(ks.lookup(s_owui), "throughput"))

    print("\n[4] strict endpoint/class pairing for a MULTI-class key")
    # HireDesk on the sync endpoint declaring interactive -> allowed.
    ok, why = endpoint_class_ok(hd, "interactive", is_async_endpoint=False)
    check("hiredesk interactive on /api/chat allowed", ok is True)
    # HireDesk on the async endpoint declaring throughput -> allowed.
    ok, why = endpoint_class_ok(hd, "throughput", is_async_endpoint=True)
    check("hiredesk throughput on /api/jobs allowed", ok is True)
    # HireDesk declaring interactive on the ASYNC endpoint -> wrong transport.
    ok, why = endpoint_class_ok(hd, "interactive", is_async_endpoint=True)
    check("hiredesk interactive on /api/jobs refused (wrong transport)", ok is False)
    # HireDesk declaring throughput on the SYNC endpoint -> wrong transport.
    ok, why = endpoint_class_ok(hd, "throughput", is_async_endpoint=False)
    check("hiredesk throughput on /api/chat refused (wrong transport)", ok is False)
    # HireDesk declaring deadline (not in its set) -> not permitted.
    ok, why = endpoint_class_ok(hd, "deadline", is_async_endpoint=True)
    check("hiredesk deadline refused (not in allowed set)", ok is False)
    check("refusal reason mentions permission", "not permitted" in (why or ""))

    print("\n[5] single-class key still strictly bound")
    owui = ks.lookup(s_owui)
    ok, _ = endpoint_class_ok(owui, "interactive", is_async_endpoint=False)
    check("openwebui interactive on /api/chat allowed", ok is True)
    ok, why = endpoint_class_ok(owui, "throughput", is_async_endpoint=True)
    check("openwebui throughput refused (not in its set)", ok is False)

    print("\n[6] the secret is never recoverable")
    listed = ks.list_keys()
    check("list_keys never exposes secret or hash",
          all("key_hash" not in k for k in listed))
    hd_row = next(k for k in listed if k["client"] == "hiredesk")
    check("list shows prefix", hd_row["key_prefix"] == s_hd[:8])
    check("list shows allowed_classes as tuple",
          isinstance(hd_row["allowed_classes"], tuple))

    print("\n[7] wrong secret does not authenticate")
    check("random token -> None", ks.lookup("nope") is None)
    check("near-miss -> None", ks.lookup(s_hd + "x") is None)

    print("\n[8] hashing")
    check("deterministic", hash_key(s_hd) == hash_key(s_hd))
    check("sha-256 length", len(hash_key(s_hd)) == 64)

    print("\n[9] validation")
    try:
        ks.create("bad", ["interactive", "bogus"], "normal")
        check("invalid class in set rejected", False)
    except ValueError:
        check("invalid class in set rejected", True)
    try:
        ks.create("empty", [], "normal")
        check("empty class set rejected", False)
    except ValueError:
        check("empty class set rejected", True)
    try:
        ks.create("badw", ["throughput"], "platinum")
        check("bad weight rejected", False)
    except ValueError:
        check("bad weight rejected", True)
    try:
        ks.create("openwebui", ["interactive"], "normal")
        check("duplicate client rejected", False)
    except ValueError:
        check("duplicate client rejected", True)

    print("\n[10] normalize helper")
    check("comma-string parses", _normalize_classes("throughput,interactive") ==
          ("interactive", "throughput"))
    check("dedupes", _normalize_classes(["throughput", "throughput"]) ==
          ("throughput",))
    check("sorts", _normalize_classes(["throughput", "deadline"]) ==
          ("deadline", "throughput"))

    print("\n[11] revoke is final and does not resurrect")
    s_tmp = ks.create("temp", ["throughput"], "low")
    check("temp works", ks.lookup(s_tmp) is not None)
    check("revoke succeeds", ks.revoke("temp") is True)
    check("revoked key dead", ks.lookup(s_tmp) is None)
    ks2 = KeyStore(db_path=ks.db_path)
    check("stays dead across store reopen (no resurrection)",
          ks2.lookup(s_tmp) is None)

    print("\n[12] update class-set without reissuing secret")
    s_at = ks.create("asset-tracker", ["throughput"], "high")
    check("update adds interactive",
          ks.update("asset-tracker", allowed_classes=["throughput", "interactive"]) is True)
    at = ks.lookup(s_at)
    check("class set updated, same secret valid",
          set(at["allowed_classes"]) == {"throughput", "interactive"})
    check("weight preserved", at["weight"] == "high")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

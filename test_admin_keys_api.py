"""
test_admin_keys_api.py — exercise admin_keys_api.py end to end with a real
FastAPI app.

Mounts the router on a throwaway app, injects a fake require_auth (toggled by
an x-test-auth header so both admin and non-admin paths are covered), points
the store at a temp DB. Touches nothing real — see the CAUTION in
admin_keys_api.py's header about why this must never run against the live
keys.db. Run: python3 test_admin_keys_api.py
"""

import os
import tempfile

from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient

import keystore
import admin_keys_api

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


def fake_require_auth(request: Request, role: str = None):
    # Mimics main.py's require_auth(request, role) status codes exactly, via a
    # test-only header instead of a real JWT cookie:
    #   x-test-auth: admin (default) | user | none
    marker = request.headers.get("x-test-auth", "admin")
    if marker == "none":
        raise HTTPException(status_code=401, detail="Not authenticated")
    if role == "admin" and marker != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"role": marker}


def build_app():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "admin_keys_test.db")
    store = keystore.KeyStore(db_path=db)
    app = FastAPI()
    admin_keys_api.init(store, require_auth=fake_require_auth)
    app.include_router(admin_keys_api.router)
    return TestClient(app), store


def main():
    c, store = build_app()

    print("\n[1] create a key")
    r = c.post("/admin/keys", json={
        "client": "openwebui", "allowed_classes": ["interactive"], "weight": "normal",
    })
    check("create returns 200", r.status_code == 200)
    body = r.json()
    check("secret returned once", isinstance(body["secret"], str) and len(body["secret"]) > 30)
    check("client echoed", body["client"] == "openwebui")
    check("allowed_classes echoed", list(body["allowed_classes"]) == ["interactive"])
    check("weight echoed", body["weight"] == "normal")
    check("store actually has the row", store.count() == 1)

    print("\n[2] create a multi-class key with capability (HireDesk shape)")
    r = c.post("/admin/keys", json={
        "client": "hiredesk", "allowed_classes": ["interactive", "throughput"],
        "weight": "normal", "capability": "high",
    })
    check("create returns 200", r.status_code == 200)
    hd = r.json()
    check("multi-class stored", set(hd["allowed_classes"]) == {"interactive", "throughput"})
    check("capability stored", hd["capability"] == "high")

    print("\n[3] validation on create")
    r = c.post("/admin/keys", json={"allowed_classes": ["interactive"], "weight": "normal"})
    check("missing client -> 400", r.status_code == 400)
    r = c.post("/admin/keys", json={"client": "x", "weight": "normal"})
    check("missing allowed_classes -> 400", r.status_code == 400)
    r = c.post("/admin/keys", json={"client": "x", "allowed_classes": ["interactive"]})
    check("missing weight -> 400", r.status_code == 400)
    r = c.post("/admin/keys", json={
        "client": "x", "allowed_classes": ["bogus"], "weight": "normal"})
    check("invalid class -> 400", r.status_code == 400)
    r = c.post("/admin/keys", json={
        "client": "x", "allowed_classes": ["interactive"], "weight": "platinum"})
    check("invalid weight -> 400", r.status_code == 400)

    print("\n[4] duplicate client -> 409, not 400")
    r = c.post("/admin/keys", json={
        "client": "openwebui", "allowed_classes": ["interactive"], "weight": "normal"})
    check("duplicate client -> 409", r.status_code == 409)

    print("\n[5] list never exposes the secret or hash")
    r = c.get("/admin/keys")
    check("list returns 200", r.status_code == 200)
    keys = r.json()["keys"]
    check("both clients present", {k["client"] for k in keys} == {"openwebui", "hiredesk"})
    check("no secret in any row", all("secret" not in k for k in keys))
    check("no key_hash in any row", all("key_hash" not in k for k in keys))
    hd_row = next(k for k in keys if k["client"] == "hiredesk")
    check("prefix present and matches the issued secret",
          hd["secret"].startswith(hd_row["key_prefix"]))

    print("\n[6] update allowed_classes without reissuing the secret")
    r = c.patch("/admin/keys/openwebui", json={"allowed_classes": ["interactive", "throughput"]})
    check("update returns 200", r.status_code == 200)
    updated = r.json()
    check("class set updated", set(updated["allowed_classes"]) == {"interactive", "throughput"})
    check("weight unchanged", updated["weight"] == "normal")
    check("original secret still authenticates (not reissued)",
          store.lookup(body["secret"]) is not None)

    print("\n[7] update weight only")
    r = c.patch("/admin/keys/hiredesk", json={"weight": "high"})
    check("weight update returns 200", r.status_code == 200)
    check("weight changed", r.json()["weight"] == "high")
    check("capability untouched", r.json()["capability"] == "high")

    print("\n[8] update validation")
    r = c.patch("/admin/keys/hiredesk", json={"weight": "platinum"})
    check("invalid weight on update -> 400", r.status_code == 400)
    r = c.patch("/admin/keys/hiredesk", json={})
    check("empty body -> 400 (nothing to update)", r.status_code == 400)
    r = c.patch("/admin/keys/does-not-exist", json={"weight": "low"})
    check("update of unknown client -> 404", r.status_code == 404)

    print("\n[9] revoke is final")
    r = c.delete("/admin/keys/openwebui")
    check("revoke returns 200", r.status_code == 200)
    check("store no longer has the row", store.count() == 1)
    r = c.get("/admin/keys")
    check("revoked client gone from list", "openwebui" not in {k["client"] for k in r.json()["keys"]})
    r = c.delete("/admin/keys/openwebui")
    check("revoking again -> 404", r.status_code == 404)

    print("\n[10] admin auth is enforced on every route")
    endpoints = [
        ("post", "/admin/keys", {"client": "z", "allowed_classes": ["interactive"], "weight": "normal"}),
        ("get", "/admin/keys", None),
        ("patch", "/admin/keys/hiredesk", {"weight": "low"}),
        ("delete", "/admin/keys/hiredesk", None),
    ]
    for method, path, payload in endpoints:
        call = getattr(c, method)
        kwargs = {"headers": {"x-test-auth": "none"}}
        if payload is not None:
            kwargs["json"] = payload
        r = call(path, **kwargs)
        check(f"{method.upper()} {path} with no auth -> 401", r.status_code == 401)
        kwargs = {"headers": {"x-test-auth": "user"}}
        if payload is not None:
            kwargs["json"] = payload
        r = call(path, **kwargs)
        check(f"{method.upper()} {path} with non-admin -> 403", r.status_code == 403)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

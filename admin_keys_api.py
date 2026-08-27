"""
admin_keys_api.py — admin CRUD endpoints for the hashed key store (v2, step 2c).

A mountable APIRouter over keystore.py's create/list_keys/revoke/update. This is
the last built piece before the cutover (see 04-remaining-work.md §2c/§2f): once
this is live, the cutover itself is done by calling POST /admin/keys for each
real client THROUGH THIS API, per the mapping in 03-gateway-v2-design.md §7 —
not a one-off script. That is deliberate: it proves the real operational path
(the same one the future Settings UI will use) rather than bypassing it.

Wiring into main.py (three lines, same pattern as jobs_api.py — see its header):

    import admin_keys_api
    admin_keys_api.init(KEY_STORE, require_auth=require_auth)   # in startup(), after KEY_STORE exists
    app.include_router(admin_keys_api.router)                    # after app = FastAPI(...)

Auth reuses main.py's require_auth(request, role="admin") (the JWT dashboard
cookie check) via dependency injection set in init(), so this module does not
duplicate session logic — the same reason jobs_api injects check_api_key rather
than importing main.

Mounted at /admin/keys*, a NEW path. The old /admin/config/keys* endpoints
(tier/env/config-based) are left in place, untouched, non-authoritative.

Does NOT touch endpoint_class_ok / enforce_endpoint_class — enforcing the
class/endpoint pairing at request time is a separate remaining-work item. This
module only manages key rows.

CAUTION (operational, not a code concern): the moment keys.db holds >= 1 row,
gwauth's empty-keystore safety net stops applying — the legacy env/config
fallback is no longer consulted for ANY client, not just the one just created.
So the first real POST /admin/keys call against the live server's keys.db is
the cutover, for all traffic at once. Build/test against temp DBs only until
ready to do the real reissue.
"""

import logging

from fastapi import APIRouter, Request, HTTPException

import keystore

log = logging.getLogger("gateway")

router = APIRouter()

# --- Module state, set by init() --------------------------------------------
_store: keystore.KeyStore = None
_require_auth = None   # injected from main.py: require_auth(request, role=None)


def init(key_store, require_auth=None):
    """
    Called once from main.py startup(), after KEY_STORE is opened. key_store is
    the SAME KeyStore instance gwauth uses — one store, one set of rows, no
    second connection pool to reason about. require_auth is main.py's JWT-cookie
    admin check, injected to avoid a circular import (main.py imports this
    module).
    """
    global _store, _require_auth
    _store = key_store
    _require_auth = require_auth


def _current_record(client):
    """Fetch a client's current record via list_keys (never exposes the hash)."""
    return next((k for k in _store.list_keys() if k["client"] == client), None)


@router.post("/admin/keys")
async def create_key(request: Request):
    _require_auth(request, role="admin")
    body = await request.json()
    client = (body.get("client") or "").strip()
    allowed_classes = body.get("allowed_classes")
    weight = body.get("weight")
    capability = body.get("capability")
    if not client:
        raise HTTPException(status_code=400, detail="client is required")
    if not allowed_classes:
        raise HTTPException(status_code=400, detail="allowed_classes is required")
    if not weight:
        raise HTTPException(status_code=400, detail="weight is required")
    try:
        secret = _store.create(client, allowed_classes, weight, capability=capability)
    except ValueError as e:
        msg = str(e)
        status = 409 if "already has a key" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    rec = _current_record(client)
    log.info(f"Admin key created for client: {client}")
    return {
        "success": True,
        "client": client,
        "secret": secret,
        "allowed_classes": rec["allowed_classes"] if rec else (),
        "weight": rec["weight"] if rec else weight,
        "capability": rec["capability"] if rec else capability,
    }


@router.get("/admin/keys")
async def list_keys(request: Request):
    _require_auth(request, role="admin")
    return {"keys": _store.list_keys()}


@router.delete("/admin/keys/{client}")
async def revoke_key(request: Request, client: str):
    _require_auth(request, role="admin")
    ok = _store.revoke(client)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No key found for {client}")
    if _store.count() == 0:
        log.critical(
            f"AUTH: revoking '{client}' emptied the keystore — ALL clients now fall "
            f"back to legacy env/config auth until a new key is created.")
    else:
        log.info(f"Admin key revoked for client: {client}")
    return {"success": True, "client": client}


@router.patch("/admin/keys/{client}")
async def update_key(request: Request, client: str):
    _require_auth(request, role="admin")
    body = await request.json()
    allowed_classes = body.get("allowed_classes")
    weight = body.get("weight")
    capability = body.get("capability")
    if allowed_classes is None and weight is None and capability is None:
        raise HTTPException(status_code=400, detail="nothing to update")
    try:
        ok = _store.update(client, allowed_classes=allowed_classes, weight=weight,
                            capability=capability)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"No key found for {client}")
    log.info(f"Admin key updated for client: {client}")
    return {"success": True, **_current_record(client)}

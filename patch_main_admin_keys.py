#!/usr/bin/env python3
"""
patch_main_admin_keys.py — mount admin_keys_api.py (the /admin/keys* CRUD
endpoints) into main.py. Step 2c's remaining piece (see
docs/04-remaining-work.md §2c, docs/00-START-HERE.md work-in-progress).

Run from ~/ai-stack/gateway:  python3 patch_main_admin_keys.py

DEPENDS ON patch_main_gwauth.py already having been applied (KEY_STORE global,
gwauth wiring in startup()). It already is, on the server, verified live
2026-07-29 — that's what makes this script's anchors safe to run there next.
If run against a main.py where that patch hasn't landed, the STARTUP_INIT
anchor below simply won't be found and this script refuses to patch (same
missing-anchor guard as every other patch script here) rather than doing
anything partial.

Same safety model as the other patch scripts: timestamped backup, exact-anchor
matching, idempotent, compile-gated to /tmp before swap, no gateway restart.

Requires admin_keys_api.py already present (built and unit-tested separately —
test_admin_keys_api.py).

Edits:
  1. `import admin_keys_api` next to the other v2 imports.
  2. `admin_keys_api.init(KEY_STORE, require_auth=require_auth)` in startup(),
     right after the gwauth wiring block (so KEY_STORE already exists).
  3. `app.include_router(admin_keys_api.router)` next to the jobs_api mount.

NOT part of this patch:
  - Anything about endpoint_class_ok / enforce_endpoint_class — that's a
    separate remaining-work item, unrelated to CRUD on key rows.
  - The old /admin/config/keys* endpoints are untouched, left mounted,
    non-authoritative.

CAUTION (operational, not a patching concern): once this is live, the FIRST
POST /admin/keys call against the real keys.db is the cutover for ALL clients
at once (gwauth's empty-keystore safety net stops applying the moment the
store holds >= 1 row) — see admin_keys_api.py's header. Deploying this patch
does not by itself do anything to live traffic; calling the endpoint does.
"""

import sys
import time
import py_compile

MAIN = "main.py"

STARTUP_INIT_GWAUTH = '''    global KEY_STORE
    KEY_STORE = keystore.KeyStore()
    gwauth.init(KEY_STORE, require_api_key=REQUIRE_API_KEY, legacy_lookup=get_key_info)
'''

STARTUP_INIT_ADMIN_KEYS = '''    admin_keys_api.init(KEY_STORE, require_auth=require_auth)
'''

# Each entry: (anchor, insertion, marker, mode)
#   mode "after" -> replace anchor with anchor + insertion
EDITS = [
    (
        "import gwauth\n",
        "import admin_keys_api\n",
        "import admin_keys_api",
        "after",
    ),
    (
        STARTUP_INIT_GWAUTH,
        STARTUP_INIT_ADMIN_KEYS,
        "admin_keys_api.init(KEY_STORE",
        "after",
    ),
    (
        "app.include_router(jobs_api.router)\n",
        "app.include_router(admin_keys_api.router)\n",
        "app.include_router(admin_keys_api.router)",
        "after",
    ),
]


def main():
    try:
        src = open(MAIN).read()
    except FileNotFoundError:
        print(f"ERROR: {MAIN} not found. Run from ~/ai-stack/gateway.")
        return 1

    already = [m for (_, _, m, _) in EDITS if m in src]
    if len(already) == len(EDITS):
        print("All admin-keys edits already present. Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected:")
        for (_, _, m, _) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m}")
        print("Refusing to patch a half-edited file. Restore a .bak and re-run.")
        return 2

    missing = [a for (a, _, _, _) in EDITS if a not in src]
    if missing:
        print("ERROR: anchor(s) not found. Does main.py already have the gwauth "
              "patch (patch_main_gwauth.py) applied? That must land first.")
        for a in missing:
            print("   ----\n" + a.rstrip() + "\n   ----")
        return 3
    for a in [a for (a, _, _, _) in EDITS]:
        if src.count(a) > 1:
            print(f"ERROR: anchor appears {src.count(a)} times, not 1 — refusing an ambiguous patch:")
            print("   ----\n" + a.rstrip() + "\n   ----")
            return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-adminkeys-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src
    for anchor, insertion, _, mode in EDITS:
        if mode == "after":
            patched = patched.replace(anchor, anchor + insertion, 1)
        else:  # replace
            patched = patched.replace(anchor, insertion, 1)

    tmp = "/tmp/main_admin_keys_check.py"
    open(tmp, "w").write(patched)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        print("ERROR: patched file failed to compile — NOT written.")
        print(e)
        print(f"Original {MAIN} untouched. Backup at {bak}.")
        return 4

    open(MAIN, "w").write(patched)
    print("Patched main.py successfully (compiles clean).")
    print(f"\nReview:   diff {bak} {MAIN}")
    print("Test first (in container): docker compose exec fastapi-gateway python3 /app/test_admin_keys_api.py")
    print("Restart:  cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())

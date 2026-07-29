#!/usr/bin/env python3
"""
patch_main_gwauth.py — wire gwauth (keystore-backed auth) into main.py's
check_api_key, safely. This is step 2c's activation piece (see
docs/00-START-HERE.md, docs/04-remaining-work.md §2c).

Run from ~/ai-stack/gateway:  python3 patch_main_gwauth.py

Same safety model as the earlier patch scripts: timestamped backup, exact-anchor
matching, idempotent, compile-gated to /tmp before swap, no gateway restart.

Requires gwauth.py + keystore.py already present (built and unit-tested
separately — test_gwauth.py, test_keystore.py).

Edits:
  1. `import keystore` + `import gwauth` next to the other v2 imports.
  2. A module-level `KEY_STORE = None` global (set in startup()).
  3. In startup(): open the keystore and call gwauth.init(...), before
     jobs_api.init() (which holds a reference to check_api_key, called later
     per-request, so exact ordering here isn't load-bearing — done first anyway
     for readability).
  4. Replace check_api_key()'s body to delegate to gwauth.authenticate(),
     translating AuthError -> HTTPException. The (client, tier) tuple contract
     is preserved EXACTLY so every existing call site keeps working unchanged.
  5. Re-sync gwauth in /admin/config/require-api-key: that handler mutates the
     REQUIRE_API_KEY global at runtime, but gwauth caches its own copy at
     init() time. Without this, toggling the dashboard flag after this patch
     lands would silently stop working.

NOT part of this patch (separate remaining-work items):
  - enforce_endpoint_class() is not called anywhere yet.
  - No admin key create/list/revoke/update endpoints.
  - jobs_api.py is untouched — it already holds check_api_key by reference.

Safety net: while keys.db is empty (true immediately after this deploys, until
keys are created), gwauth falls back to the exact old env/config lookup
(get_key_info) with the same status codes — but logs a CRITICAL once and a
WARNING on every request until the store is populated. Expect log volume on
live /api/chat traffic until keys exist.
"""

import sys
import time
import py_compile

MAIN = "main.py"

OLD_CHECK_API_KEY = '''def check_api_key(request: Request) -> tuple:
    """Returns (client_name, tier) if key is valid, or ('anonymous', 2) if keys not required."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth.removeprefix("Bearer ").strip()
        info = get_key_info(key)
        if info:
            return info["name"], info["tier"]
        if REQUIRE_API_KEY:
            raise HTTPException(status_code=403, detail="Invalid API key")
        return "anonymous", 2
    if REQUIRE_API_KEY:
        raise HTTPException(status_code=401, detail="Missing API key")
    return "anonymous", 2
'''

NEW_CHECK_API_KEY = '''def check_api_key(request: Request) -> tuple:
    """Returns (client_name, tier). Delegates to gwauth (keystore-backed auth);
    falls back to the legacy env/config lookup only while keys.db is empty
    (see gwauth.py's empty-keystore safety net)."""
    try:
        rec = gwauth.authenticate(request.headers)
    except gwauth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return rec["client"], rec["tier"]
'''

STARTUP_INIT = '''    global KEY_STORE
    KEY_STORE = keystore.KeyStore()
    gwauth.init(KEY_STORE, require_api_key=REQUIRE_API_KEY, legacy_lookup=get_key_info)
'''

TOGGLE_RESYNC = '''    # gwauth caches REQUIRE_API_KEY at init() time; keep it in sync with the
    # live toggle above or this dashboard switch silently stops doing anything.
    gwauth.init(KEY_STORE, require_api_key=REQUIRE_API_KEY, legacy_lookup=get_key_info)
'''

# Each entry: (anchor, insertion, marker, mode)
#   mode "after"   -> replace anchor with anchor + insertion
#   mode "replace" -> replace anchor with insertion (anchor IS the old text)
EDITS = [
    (
        "import admission\n",
        "import keystore\nimport gwauth\n",
        "import gwauth",
        "after",
    ),
    (
        "API_KEYS = {}\n",
        "KEY_STORE = None  # keystore.KeyStore instance; set in startup()\n",
        "KEY_STORE = None  # keystore.KeyStore instance; set in startup()",
        "after",
    ),
    (
        "    jobs_api.init(CONFIG, check_api_key=check_api_key, free_slots_fn=_count_free_slots)\n",
        STARTUP_INIT,
        "KEY_STORE = keystore.KeyStore()",
        "before",
    ),
    (
        OLD_CHECK_API_KEY,
        NEW_CHECK_API_KEY,
        "Delegates to gwauth (keystore-backed auth)",
        "replace",
    ),
    (
        '    REQUIRE_API_KEY = value\n    CONFIG["require_api_key"] = value\n',
        '    REQUIRE_API_KEY = value\n    CONFIG["require_api_key"] = value\n' + TOGGLE_RESYNC,
        "silently stops doing anything",
        "replace",
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
        print("All gwauth edits already present. Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected:")
        for (_, _, m, _) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m}")
        print("Refusing to patch a half-edited file. Restore a .bak and re-run.")
        return 2

    missing = [a for (a, _, _, _) in EDITS if a not in src]
    if missing:
        print("ERROR: anchor(s) not found. Does main.py match the version this script targets?")
        for a in missing:
            print("   ----\n" + a.rstrip() + "\n   ----")
        return 3
    for a in [a for (a, _, _, _) in EDITS]:
        if src.count(a) > 1:
            print(f"ERROR: anchor appears {src.count(a)} times, not 1 — refusing an ambiguous patch:")
            print("   ----\n" + a.rstrip() + "\n   ----")
            return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-gwauth-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src
    for anchor, insertion, _, mode in EDITS:
        if mode == "after":
            patched = patched.replace(anchor, anchor + insertion, 1)
        elif mode == "before":
            patched = patched.replace(anchor, insertion + anchor, 1)
        else:  # replace
            patched = patched.replace(anchor, insertion, 1)

    tmp = "/tmp/main_gwauth_check.py"
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
    print("Test first (in container): docker compose exec fastapi-gateway python3 /app/test_gwauth.py")
    print("Restart:  cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())

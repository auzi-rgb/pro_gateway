#!/usr/bin/env python3
"""
patch_main_admin_auth_order.py — fix an auth-ordering bug in main.py's
/admin/nodes and /admin/models: both currently run a forced, unthrottled
health-check burst against every GPU node BEFORE require_auth() checks the
caller has a valid session. Since check_all_nodes(force=True) bypasses the
normal 30s throttle entirely, any request to either path -- authenticated or
not -- unconditionally fires two HTTP calls per node before the 401 would
even be raised. Fix: require_auth() first, then the health check, in both
handlers. No other behavior changes; the auth level itself (any logged-in
user, no role= given) is unchanged.

Run from ~/ai-stack/gateway:  python3 patch_main_admin_auth_order.py

Same safety model as the other patch scripts here: timestamped backup,
exact-anchor matching, idempotent, compile-gated to /tmp before swap, no
gateway restart.

Both edits are pure line-swaps with no other changes. The two-line body
(`await check_all_nodes(force=True)` / `require_auth(request)`) is IDENTICAL
text in both handlers, so each anchor below includes its function's `async
def ...` signature line to keep the match unique -- a bare anchor on just the
two lines would match twice and trip the same ambiguous-anchor refusal the
other patch scripts already have.
"""

import sys
import time
import py_compile

MAIN = "main.py"

# Each entry: (anchor, replacement, marker, mode)
#   mode "replace" -> anchor IS the old text; replaced wholesale by `replacement`
EDITS = [
    (
        'async def admin_nodes(request: Request):\n'
        '    await check_all_nodes(force=True)\n'
        '    require_auth(request)\n',
        'async def admin_nodes(request: Request):\n'
        '    require_auth(request)\n'
        '    await check_all_nodes(force=True)\n',
        'async def admin_nodes(request: Request):\n    require_auth(request)\n    await check_all_nodes',
        "replace",
    ),
    (
        'async def admin_models(request: Request):\n'
        '    await check_all_nodes(force=True)\n'
        '    require_auth(request)\n',
        'async def admin_models(request: Request):\n'
        '    require_auth(request)\n'
        '    await check_all_nodes(force=True)\n',
        'async def admin_models(request: Request):\n    require_auth(request)\n    await check_all_nodes',
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
        print("Both edits already present. Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected:")
        for (_, _, m, _) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m!r}")
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
    bak = f"{MAIN}.bak-authorder-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src
    for anchor, replacement, _, mode in EDITS:
        patched = patched.replace(anchor, replacement, 1)

    tmp = "/tmp/main_admin_auth_order_check.py"
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
    print("Restart:  cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())

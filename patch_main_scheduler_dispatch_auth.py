#!/usr/bin/env python3
"""
patch_main_scheduler_dispatch_auth.py — add the missing auth check to
main.py's /admin/scheduler/dispatch (audit finding #6). Unlike its sibling
/admin/scheduler ("Public - live scheduler/queue state..."), this endpoint
was never marked public and never called require_auth -- an oversight, not
a deliberate public endpoint: it's not called by templates/live.html (the
one page that's genuinely meant to be unauthenticated) or dashboard.html,
and every other read-only "detail/stats/logs" GET endpoint in this file
(/admin/nodes, /admin/routing, /admin/stats/node/{}, /admin/logs,
/admin/models) requires a logged-in session via a bare require_auth(request)
-- no role=, since role="admin" here is reserved for state-mutating
endpoints. This brings /admin/scheduler/dispatch in line with that pattern.

Run from ~/ai-stack/gateway:  python3 patch_main_scheduler_dispatch_auth.py

Same safety model as the other patch scripts here: timestamped backup,
exact-anchor matching, idempotent, compile-gated to /tmp before swap, no
gateway restart.
"""

import sys
import time
import py_compile

MAIN = "main.py"

OLD = (
    'async def admin_scheduler_dispatch(request: Request, limit: int = 200):\n'
    '    """Recent dispatch decisions - who won each slot, at what score, and what they beat."""\n'
    '    recent = list(_dispatch_log)[-limit:]\n'
)
NEW = (
    'async def admin_scheduler_dispatch(request: Request, limit: int = 200):\n'
    '    """Recent dispatch decisions - who won each slot, at what score, and what they beat."""\n'
    '    require_auth(request)\n'
    '    recent = list(_dispatch_log)[-limit:]\n'
)


def main():
    try:
        src = open(MAIN).read()
    except FileNotFoundError:
        print(f"ERROR: {MAIN} not found. Run from ~/ai-stack/gateway.")
        return 1

    if NEW in src:
        print("Already applied. Nothing to do.")
        return 0

    if OLD not in src:
        print("ERROR: anchor not found. Does main.py match the version this script targets?")
        print("   ----\n" + OLD.rstrip() + "\n   ----")
        return 3

    if src.count(OLD) > 1:
        print(f"ERROR: anchor appears {src.count(OLD)} times, not 1 — refusing an ambiguous patch.")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-schedulerdispatchauth-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src.replace(OLD, NEW, 1)

    tmp = "/tmp/main_scheduler_dispatch_auth_check.py"
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

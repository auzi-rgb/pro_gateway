#!/usr/bin/env python3
"""
patch_main_admission.py — wire admission control into main.py, safely.

Run from ~/ai-stack/gateway:  python3 patch_main_admission.py

Same safety model as the earlier patch scripts: timestamped backup, exact-anchor
matching, idempotent, compile-gated to /tmp before swap, no gateway restart.

Requires the jobs_api + dispatcher wiring already present.

Edits:
  1. `import admission` next to the other v2 imports.
  2. A module-level `_count_free_slots()` helper (uses the same slot accounting
     the dispatcher and chat scheduler use: free = sum(max_concurrent -
     active_requests) over healthy, enabled, non-circuit-broken nodes).
  3. In startup(): `admission.init(CONFIG)`, and pass `free_slots_fn=` to the
     existing `jobs_api.init(...)` call so the submit path can estimate wait.
"""

import sys
import time
import py_compile

MAIN = "main.py"

FREE_SLOTS_HELPER = '''

def _count_free_slots() -> int:
    """
    Current number of free node slots across the fleet, using the same accounting
    the dispatcher and chat scheduler use. Passed to jobs_api for admission
    control's wait estimate. Live and self-correcting: a down/circuit-broken node
    contributes zero, so the estimate reflects real capacity.
    """
    free = 0
    for _n in nodes:
        if not _n.enabled or not _n.healthy:
            continue
        if _n.circuit_open_until > time.time():
            continue
        free += max(0, _n.max_concurrent - _n.active_requests)
    return free
'''

EDITS = [
    # Import + free-slots helper as ONE insertion, anchored on the pre-existing
    # `import dispatcher` line. (Chaining an edit onto a string a previous edit
    # creates is fragile — anchors must exist in the original file.)
    (
        "import dispatcher\n",
        "import admission\n" + FREE_SLOTS_HELPER,
        "_count_free_slots",
    ),
    # Add free_slots_fn to the existing jobs_api.init call (rewrites that line).
    (
        "    jobs_api.init(CONFIG, check_api_key=check_api_key)\n",
        "    jobs_api.init(CONFIG, check_api_key=check_api_key, "
        "free_slots_fn=_count_free_slots)\n",
        "free_slots_fn=_count_free_slots)",
    ),
    # Initialize admission in startup, right after jobs_api store init.
    (
        "    await jobs_api.start_prune_task()\n",
        "    admission.init(CONFIG)\n",
        "admission.init(CONFIG)",
    ),
]


def main():
    try:
        src = open(MAIN).read()
    except FileNotFoundError:
        print(f"ERROR: {MAIN} not found. Run from ~/ai-stack/gateway.")
        return 1

    already = [m for (_, _, m) in EDITS if m in src]
    if len(already) == len(EDITS):
        print("All admission edits already present. Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected:")
        for (_, _, m) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m}")
        print("Refusing to patch a half-edited file. Restore a .bak and re-run.")
        return 2

    missing = [a for (a, _, _) in EDITS if a not in src]
    if missing:
        print("ERROR: anchor(s) not found. Requires jobs_api + dispatcher wiring first.")
        for a in missing:
            print("   ----\n" + a.rstrip() + "\n   ----")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-admission-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    # Apply the first jobs_api.init edit carefully: the free_slots_fn edit and
    # the admission.init edit both anchor on lines near it, and the
    # `import admission` insertion happens twice-anchored (import + helper).
    # Apply in order, each as a single replace.
    patched = src
    for anchor, insertion, _ in EDITS:
        # Insert AFTER the anchor line, except the jobs_api.init edit which
        # REPLACES the anchor line (it rewrites that call).
        if insertion.strip().startswith("jobs_api.init("):
            patched = patched.replace(anchor, insertion, 1)
        else:
            patched = patched.replace(anchor, anchor + insertion, 1)

    tmp = "/tmp/main_admission_check.py"
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

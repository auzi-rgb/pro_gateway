#!/usr/bin/env python3
"""
patch_main_dispatcher.py — wire dispatcher into main.py, safely.

Run from ~/ai-stack/gateway:  python3 patch_main_dispatcher.py

Same safety model as patch_main.py:
  - timestamped backup
  - anchors must match exactly, else no changes
  - idempotent (won't double-apply)
  - compiles the result to /tmp and only swaps in on success
  - does NOT restart the gateway

Two edits:
  1. `import dispatcher` next to the jobs_api import.
  2. In startup(), AFTER jobs_api is initialized (which is after nodes are
     populated), init and start the dispatcher. Placing it after the jobs_api
     block guarantees `nodes`, `_free_node_for`, and `DEFAULT_MODEL` all exist.
"""

import sys
import time
import py_compile

MAIN = "main.py"

EDITS = [
    (
        "import jobs_api\n",
        "import dispatcher\n",
        "import dispatcher",
    ),
    (
        "    jobs_api.init(CONFIG, check_api_key=check_api_key)\n"
        "    await jobs_api.start_prune_task()\n",
        "    # --- v2 async dispatcher: runs queued jobs across the fleet ---\n"
        "    dispatcher.init(CONFIG, jobs_api.STORE, nodes, _free_node_for, DEFAULT_MODEL)\n"
        "    await dispatcher.start()\n",
        "dispatcher.init(CONFIG",
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
        print("Both dispatcher edits already present. Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected:")
        for (_, _, m) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m}")
        print("Refusing to patch a half-edited file. Restore a .bak and re-run.")
        return 2

    missing = [a for (a, _, _) in EDITS if a not in src]
    if missing:
        print("ERROR: anchor(s) not found — main.py differs from expected.")
        print("This patch requires the jobs_api wiring (patch_main.py) applied first.")
        for a in missing:
            print("   ----\n" + a + "   ----")
        return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-dispatcher-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src
    for anchor, insertion, _ in EDITS:
        patched = patched.replace(anchor, anchor + insertion, 1)

    tmp = "/tmp/main_dispatcher_check.py"
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

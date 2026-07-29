#!/usr/bin/env python3
"""
patch_main.py — wire jobs_api into main.py, safely.

Run from ~/ai-stack/gateway:   python3 patch_main.py

What it does:
  1. Reads main.py, makes a timestamped .bak.
  2. Applies three edits by matching exact anchor strings.
  3. Refuses to run if an anchor is missing or an edit is already present
     (idempotent — safe to run twice).
  4. Compiles the patched result to /tmp BEFORE writing it back.
  5. Only overwrites main.py if compilation succeeds.

It does NOT restart the gateway. You do that yourself after reviewing.
"""

import sys
import time
import py_compile

MAIN = "main.py"

# (anchor that must exist, text to insert AFTER it, a marker proving it's done)
EDITS = [
    (
        "from fastapi.staticfiles import StaticFiles\n",
        "import jobs_api\n",
        "import jobs_api",
    ),
    (
        'app = FastAPI(title="AI Gateway")\n'
        "app.add_middleware(MaxBodySizeMiddleware, max_bytes=10 * 1024 * 1024)\n",
        "app.include_router(jobs_api.router)\n",
        "app.include_router(jobs_api.router)",
    ),
    (
        '    log.info(f"Gateway started with {len(nodes)} nodes")\n',
        "    # --- v2 async job store: recover orphans, start prune loop ---\n"
        "    jobs_api.init(CONFIG, check_api_key=check_api_key)\n"
        "    await jobs_api.start_prune_task()\n",
        "jobs_api.init(CONFIG",
    ),
]


def main():
    try:
        src = open(MAIN).read()
    except FileNotFoundError:
        print(f"ERROR: {MAIN} not found. Run this from ~/ai-stack/gateway.")
        return 1

    # Idempotency + anchor checks first, before touching anything.
    already = [m for (_, _, m) in EDITS if m in src]
    if len(already) == len(EDITS):
        print("All three edits already present — main.py is already wired. "
              "Nothing to do.")
        return 0
    if already:
        print("PARTIAL patch detected — some edits present, some not:")
        for (_, _, m) in EDITS:
            print(f"   {'present' if m in src else 'MISSING'}: {m}")
        print("Refusing to patch a half-edited file. Restore from a .bak and "
              "re-run, or inspect manually.")
        return 2

    missing = [a for (a, _, _) in EDITS if a not in src]
    if missing:
        print("ERROR: could not find these anchor(s) in main.py — the file may "
              "have changed since this script was written:")
        for a in missing:
            print("   ----\n" + a + "   ----")
        print("No changes made.")
        return 3

    # Back up.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-jobsapi-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    # Apply edits: insert each insertion immediately after its anchor.
    patched = src
    for anchor, insertion, _ in EDITS:
        patched = patched.replace(anchor, anchor + insertion, 1)

    # Compile-gate: write to /tmp, compile, only then swap.
    tmp = "/tmp/main_patched_check.py"
    open(tmp, "w").write(patched)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        print("ERROR: patched file failed to compile — NOT written.")
        print(e)
        print(f"Your original {MAIN} is untouched. Backup at {bak}.")
        return 4

    open(MAIN, "w").write(patched)
    print("Patched main.py successfully (compiles clean).")
    print("\nReview the diff with:")
    print(f"    diff {bak} {MAIN}")
    print("\nThen restart the gateway with:")
    print("    cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())

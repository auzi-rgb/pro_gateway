#!/usr/bin/env python3
"""
patch_main_remove_routing_rules.py — remove the hard "routing rules" feature
from main.py: confirmed (via real server logs) zero traffic on every endpoint
that depended on it. See docs/04-remaining-work.md §0 ("Investigate / remove
the hard routing rules") and the audit that identified this as leftover
migration cruft, not something /api/chat (the live production path) ever
consulted.

Run from ~/ai-stack/gateway:  python3 patch_main_remove_routing_rules.py

Same safety model as the other patch scripts here: timestamped backup,
exact-anchor matching, idempotent, compile-gated to /tmp before swap, no
gateway restart.

Unlike the ADD-style patches (gwauth, admin_keys, dispatcher, ...), every
edit here is a pure REMOVAL, so the idempotency check works in reverse: an
edit is "already applied" when its anchor is ABSENT from main.py (there's
nothing left to remove), not when some new marker is present.

Edits:
  1. Delete the ROUTING_RULES global.
  2. Trim pick_node(): remove ONLY the routing-rule override block. The
     tier-based phases (preferred -> fallback -> any healthy node) are
     independent of routing rules and are NOT touched -- /api/embed,
     /api/embeddings, and /v1/chat/completions still call pick_node() for
     that tier-based selection.
  3. Delete /api/generate entirely (confirmed zero traffic; fully redundant
     with /api/chat, which already covers the same capability).
  4. Delete all three routing-rule admin endpoints (GET .../routing-rules,
     POST .../routing-rules/model, POST .../routing-rules/client).

NOT removed: /api/embed, /api/embeddings, /v1/chat/completions (kept, wired
to the trimmed pick_node()); /admin/routing and /admin/topology (confirmed
independent -- they compute their own load-ratio maps directly and never
reference ROUTING_RULES or pick_node()).

The dashboard's "Routing Rules" card + JS is a separate, plain template edit
(templates/dashboard.html is not under this guarded-script convention -- see
00-START-HERE.md, which scopes "never hand-edit" to main.py specifically).
"""

import sys
import time
import py_compile

MAIN = "main.py"

EDITS = [
    (
        "routing_rules_global",
        'ROUTING_RULES = CONFIG.get("routing_rules", {"models": {}, "clients": {}})\n',
        "",
    ),
    (
        "pick_node_routing_override",
        '    fallback  = tier_cfg["fallback_nodes"]\n'
        '\n'
        '    # Hard routing rules override everything - client rule beats model rule\n'
        '    client_rule = ROUTING_RULES.get("clients", {}).get(client)\n'
        '    model_rule  = ROUTING_RULES.get("models",  {}).get(target_model)\n'
        '    rule_name   = client_rule or model_rule\n'
        '    if rule_name:\n'
        '        node = get_node_by_name(rule_name)\n'
        '        if node and node.enabled and node.healthy:\n'
        '            if not target_model or target_model in node.available_models:\n'
        '                return node\n'
        '\n'
        '    # Phase 1 - load-balance across all preferred nodes that can run this model\n',
        '    fallback  = tier_cfg["fallback_nodes"]\n'
        '\n'
        '    # Phase 1 - load-balance across all preferred nodes that can run this model\n',
    ),
    (
        "api_generate_endpoint",
        '\n@app.post("/api/generate")\n'
        'async def api_generate(request: Request):\n'
        '    client, tier = check_api_key(request)\n'
        '    body = await request.json()\n'
        '    model = body.get("model") or DEFAULT_MODEL\n'
        '    body["model"] = model\n'
        '    body["stream"] = False\n'
        '    await check_all_nodes(force=False)\n'
        '    node = pick_node(model, client, tier)\n'
        '    if not node:\n'
        '        log_request("/api/generate", model, None, None, 0, 0, False, 503, client, "No available nodes")\n'
        '        raise HTTPException(status_code=503, detail="No available nodes for this request")\n'
        '    source_ip = request.client.host if request.client else None\n'
        '    t_wait_start = time.time()\n'
        '    node.queued_requests += 1\n'
        '    async with node.semaphore:\n'
        '        node.queued_requests -= 1\n'
        '        t_start = time.time()\n'
        '        wait_time = t_start - t_wait_start\n'
        '        log.info(f"Routing /api/generate model={model} client={client} ip={source_ip} -> {node.name}")\n'
        '        try:\n'
        '            result = await proxy_request(node, "POST", "/api/generate", body, request, model)\n'
        '            duration = time.time() - t_start\n'
        '            log_request("/api/generate", model, model, node.name, wait_time, duration, True, 200, client, None, source_ip)\n'
        '        except Exception as e:\n'
        '            duration = time.time() - t_start\n'
        '            log_request("/api/generate", model, model, node.name, wait_time, duration, False, 500, client, str(e), source_ip)\n'
        '            raise\n'
        '    return JSONResponse(content=result)\n',
        "",
    ),
    (
        "routing_rules_admin_endpoints",
        '\n\n\n@app.get("/admin/config/routing-rules")\n'
        'async def get_routing_rules(request: Request):\n'
        '    require_auth(request)\n'
        '    return {\n'
        '        "routing_rules": ROUTING_RULES,\n'
        '        "available_nodes": [n.name for n in nodes]\n'
        '    }\n'
        '\n'
        '@app.post("/admin/config/routing-rules/model")\n'
        'async def set_model_rule(request: Request):\n'
        '    require_auth(request, role="admin")\n'
        '    global ROUTING_RULES\n'
        '    body = await request.json()\n'
        '    model = body.get("model", "").strip()\n'
        '    node_name = body.get("node", "").strip()\n'
        '    if not model:\n'
        '        raise HTTPException(status_code=400, detail="Model required")\n'
        '    if "models" not in ROUTING_RULES:\n'
        '        ROUTING_RULES["models"] = {}\n'
        '    if node_name:\n'
        '        ROUTING_RULES["models"][model] = node_name\n'
        '        log.info(f"Routing rule set: model {model} -> {node_name}")\n'
        '    else:\n'
        '        ROUTING_RULES["models"].pop(model, None)\n'
        '        log.info(f"Routing rule cleared for model {model}")\n'
        '    CONFIG["routing_rules"] = ROUTING_RULES\n'
        '    with open("/app/config.json", "w") as f:\n'
        '        json.dump(CONFIG, f, indent=2)\n'
        '    return {"success": True, "routing_rules": ROUTING_RULES}\n'
        '\n'
        '@app.post("/admin/config/routing-rules/client")\n'
        'async def set_client_rule(request: Request):\n'
        '    require_auth(request, role="admin")\n'
        '    global ROUTING_RULES\n'
        '    body = await request.json()\n'
        '    client = body.get("client", "").strip()\n'
        '    node_name = body.get("node", "").strip()\n'
        '    if not client:\n'
        '        raise HTTPException(status_code=400, detail="Client required")\n'
        '    if "clients" not in ROUTING_RULES:\n'
        '        ROUTING_RULES["clients"] = {}\n'
        '    if node_name:\n'
        '        ROUTING_RULES["clients"][client] = node_name\n'
        '        log.info(f"Routing rule set: client {client} -> {node_name}")\n'
        '    else:\n'
        '        ROUTING_RULES["clients"].pop(client, None)\n'
        '        log.info(f"Routing rule cleared for client {client}")\n'
        '    CONFIG["routing_rules"] = ROUTING_RULES\n'
        '    with open("/app/config.json", "w") as f:\n'
        '        json.dump(CONFIG, f, indent=2)\n'
        '    return {"success": True, "routing_rules": ROUTING_RULES}\n'
        '\n'
        '\n'
        '\n',
        "\n\n",
    ),
]


def main():
    try:
        src = open(MAIN).read()
    except FileNotFoundError:
        print(f"ERROR: {MAIN} not found. Run from ~/ai-stack/gateway.")
        return 1

    present = [name for (name, anchor, _) in EDITS if anchor in src]
    if not present:
        print("All edits already applied (no anchors found). Nothing to do.")
        return 0
    if len(present) != len(EDITS):
        missing = [name for (name, anchor, _) in EDITS if anchor not in src]
        print("PARTIAL patch detected:")
        for name in [n for (n, _, _) in EDITS]:
            print(f"   {'still present' if name in present else 'already removed'}: {name}")
        print("Refusing to patch a half-edited file. Restore a .bak and re-run.")
        return 2

    for name, anchor, _ in EDITS:
        if src.count(anchor) > 1:
            print(f"ERROR: anchor '{name}' appears {src.count(anchor)} times, not 1 — refusing an ambiguous patch:")
            print("   ----\n" + anchor.rstrip() + "\n   ----")
            return 3

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{MAIN}.bak-routingrules-{stamp}"
    open(bak, "w").write(src)
    print(f"Backed up {MAIN} -> {bak}")

    patched = src
    for _, anchor, replacement in EDITS:
        patched = patched.replace(anchor, replacement, 1)

    tmp = "/tmp/main_remove_routing_rules_check.py"
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

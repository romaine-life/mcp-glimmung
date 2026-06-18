# mcp-glimmung

Glimmung MCP server.

## Layout

- `src/` - Python MCP server package.
- `Dockerfile` - image build for `romainecr.azurecr.io/mcp-glimmung`.
- `chart/` - Helm chart synced by ArgoCD.

Images are SHA-tagged from `main`; `.github/workflows/build.yml` pushes the image and commits the matching chart tag.

## Test-Slot Tool Contract

Test-slot **provisioning is deterministic and server-side**: Tank's
`POST /api/sessions/{id}/test-workflow/start` button/endpoint validates
readiness and drives the `POST /v1/test-slots/checkout` +
`POST /v1/test-slots/deploy-image` Glimmung HTTP APIs from inside Tank's
backend. Those checkout/deploy HTTP endpoints stay; only the agent-facing
`checkout_test_slot` / `deploy_image_to_test_slot` MCP tool wrappers were
retired (the deterministic Test button replaces them for every project).
`tests/test_retired_test_slot_mcp_tools.py` guards against their reintroduction.

The session-facing MCP surface that remains is lifecycle/observability only:
`extend_test_slot_lease` wraps `POST /v1/test-slots/extend`; it requires the
Tank session identity so a session can renew its own active checkout without
returning and tearing down the slot namespace.
`return_test_slot` owns lease-scoped runtime cleanup.
`repair_test_slot` wraps
`POST /v1/projects/{project}/test-environments/{slot_name}/repair` for
admin revalidation of one configured, unleased slot. It does not change queue
size, choose a slot, or activate hot runtime — it is a preliminary-capacity
revalidation path, not a cleanup or reset path for active leases.
`set_test_environment_count` scales a project's reserved warm-slot capacity.

`list_leases` separates prepared lifecycle capacity from checkout admission.
`prepared_test_slots` are durable slot rows with lifecycle state `available`;
`available_test_slots` are the subset the Glimmung state projection reports as
checkout-admissible.

## Synthetic Dispatch Tool Contract

`synthetic_dispatch_run` wraps Glimmung's break-glass
`POST /v1/runs/synthetic-dispatch` endpoint. The tool is intentionally strict:
it does not infer skipped phase outputs, provision a test slot, or repair
workflow shape. Callers must provide `start_at_phase`, a claimed
`slot_lease_ref`, and every supplied phase output the entrypoint phase will
need.

For downstream process failures, callers can set `copy_phase_outputs_from` to
reuse selected outputs from an earlier run on the same issue without rerunning
agent phases:

```json
{
  "run": "17.1",
  "phases": {
    "llm-verify": ["verification"]
  }
}
```

Copied phases must be before `start_at_phase`. Explicit
`supplied_phase_outputs` may add missing keys but may not conflict with copied
keys.

For verifier recovery, prefer a typed `verification` block on the supplied
verification phase instead of a legacy `phase_outputs.verification` JSON string:

```json
{
  "phase": "llm-verify",
  "verification": {
    "status": "pass",
    "reasons": ["tooltip showed Energy generated 1"],
    "evidence_refs": [
      "runs/spirelens/<run_id>/screenshots/issue148-happy-flower-tooltip.png"
    ],
    "evidence": [
      {
        "kind": "screenshot",
        "ref": "runs/spirelens/<run_id>/screenshots/issue148-happy-flower-tooltip.png",
        "label": "Happy Flower tooltip"
      }
    ]
  }
}
```

Glimmung accepts typed supplied verification only for verification phases before
`start_at_phase`, and only with `status: "pass"` because synthetic supplied
attempts are carry-forward advances. Copying a legacy output named
`verification` remains a copied phase output and is not promoted into typed
verification.

## Authenticated Tank Browser Inspections

`inspect_browser_url` supports `tank_auth=True` for Tank UI pages. The tool
uses the already-verified inbound `auth.romaine.life` caller JWT, preflights it
against the inspected origin's `/api/auth/me`, and seeds it into
`localStorage["auth-romaine-jwt"]` before Playwright navigates. Prefer this for
Tank slots instead of manually minting, copying, or pasting JWTs.

Manual `cookies`, `extra_http_headers`, and `local_storage` injection remain
available for non-Tank apps or custom auth setups. If `tank_auth=True` conflicts
with a caller-provided `local_storage[origin]["auth-romaine-jwt"]`, the tool
fails fast rather than silently choosing one token.

When an inspection screenshot is user-facing evidence, pass
`save_screenshot_to_workspace=True`. The MCP server uploads the same PNG bytes
through Tank's session file API, which stores image uploads under
`/workspace/screenshots/` and returns the saved path in
`workspace_screenshot`. `workspace_screenshot_name` only labels the upload;
Tank still chooses the collision-safe final filename.

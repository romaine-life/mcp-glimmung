# mcp-glimmung

Glimmung MCP server.

## Layout

- `src/` - Python MCP server package.
- `Dockerfile` - image build for `romainecr.azurecr.io/mcp-glimmung`.
- `chart/` - Helm chart synced by ArgoCD.

Images are SHA-tagged from `main`; `.github/workflows/build.yml` pushes the image and commits the matching chart tag.

## Test-Slot Tool Contract

`checkout_test_slot` is the MCP wrapper for Glimmung's
`POST /v1/test-slots/checkout` API. Checkout is allocator-owned: callers pass
the project and Tank session identity, and Glimmung returns the assigned slot.
`extend_test_slot_lease` wraps `POST /v1/test-slots/extend`; it requires the
Tank session identity so a session can renew its own active checkout without
returning and tearing down the slot namespace.
`repair_test_slot` wraps
`POST /v1/projects/{project}/test-environments/{slot_name}/repair` for
admin revalidation of one configured, unleased slot. It does not change queue
size, choose a slot, or activate hot runtime.

The checkout tool must not expose or forward caller-owned slot selection or
cleanup fields such as `slot_index`, `mode`, or `phase_inputs`. Those fields
are rejected by the Glimmung API. Queue size changes own destructive capacity
changes, and `return_test_slot` owns lease-scoped runtime cleanup.
Repair is a preliminary-capacity revalidation path; it is not a cleanup or
reset path for active leases.

Checkout may return while activation is still running. When the response has
`state: "activating"` and `usable: false`, callers should poll the returned
`status_url` or `get_state` until the slot is `active` and `usable` before
using the environment.

`list_leases` separates prepared lifecycle capacity from checkout admission.
`prepared_test_slots` are durable slot rows with lifecycle state `available`;
`available_test_slots` are the subset the Glimmung state projection reports as
checkout-admissible.

## Synthetic Dispatch Tool Contract

`synthetic_dispatch_run` wraps Glimmung's break-glass
`POST /v1/runs/synthetic-dispatch` endpoint. The tool is intentionally strict:
it does not fetch prior runs, infer skipped phase outputs, provision a test
slot, or repair workflow shape. Callers must provide `start_at_phase`, a
claimed `slot_lease_ref`, and every supplied phase output the entrypoint phase
will need.

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

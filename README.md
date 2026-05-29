# mcp-glimmung

[![SafeSkill 91/100](https://img.shields.io/badge/SafeSkill-91%2F100_Verified%20Safe-brightgreen)](https://safeskill.dev/scan/nelsong6-mcp-glimmung)
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

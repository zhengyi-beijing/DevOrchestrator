# P13 Transport-Independent Control Bridge

Status: **PENDING DESIGN**

Goal: remove RDC as the normal ChatGPT-to-DevO control dependency while preserving DevOrchestrator as the sole lifecycle authority.

Scope:
- Add a Control Adapter boundary above the existing P12 Control API.
- Provide an MCPAdapter MVP for bounded semantic status, log and lifecycle-control operations.
- Stabilize WebBridgeAdapter as a secondary transport with durable request identity, freshness and fail-closed handling.
- Separate ExecutionTransport from control transport: Local first, SSH over Tailscale for remote hosts, RDC only as fallback/emergency GUI/debug transport.
- Preserve idempotency, repository-truth/revision guards, authentication/origin protections, audit and explicit stale/unknown states.
- Do not expose unrestricted remote shell through the Control API.

Acceptance:
- With RDC unavailable, a supported Control Adapter can inspect authoritative DevO state and issue bounded lifecycle controls.
- DevO can execute through Local/SSH transport without changing lifecycle authority.
- RDC remains available only as a fallback transport.

Design note: detailed executable design must be produced and independently reviewed before implementation.

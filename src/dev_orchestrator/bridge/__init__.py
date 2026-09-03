"""Browser Bridge transport for Web Sol reasoning requests.

The bridge is transport only. It persists outbound Web Sol requests under an
exact ``(adapter, binding_id)`` queue, serves the dedicated local Bridge HTTP
surface (``/v1/health``, ``/v1/claim``, ``/v1/renew``, ``/v1/response``), and
renders Web Sol request prompts for a dumb ChatGPT Web adapter. It never
validates ``validate_websol_response`` output, never starts Workers, and never
applies ``next_action``; those remain DevOrchestrator Core responsibilities.
"""

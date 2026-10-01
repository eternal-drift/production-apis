# Operations

## Running

```bash
uv sync                                   # install
uv run production-apis                    # all six APIs (dev: pretty logs, docs enabled)
ENABLED_APIS=rest,grpc uv run production-apis   # a subset
export JWT_SECRET=$(openssl rand -hex 32)
docker compose up --build                 # production mode, read-only FS, dropped capabilities
uv run python scripts/demo.py             # journey J1 narrated end to end
uv run python scripts/mint_token.py me    # dev token (refuses in production)
```

| API | Port | Probe |
|---|---|---|
| REST | 3001 | `GET /healthz`, `/readyz`, `/metrics`, `/docs` (non-prod) |
| GraphQL | 3002 | `GET /healthz`, endpoint `/graphql` |
| gRPC | 3003 | `grpc.health.v1.Health/Check` |
| WebSocket | 3004 | `GET /healthz`, endpoint `/ws` |
| Webhook mgmt | 3005 | `GET /healthz` |
| SOAP | 3006 | `GET /healthz`, `GET /soap?wsdl` |

## Production checklist

- **TLS** terminates at the ingress/mesh. gRPC needs HTTP/2 end to end (ALB/Envoy/Istio);
  WebSockets need upgrade support and idle timeouts above the heartbeat (30 s).
- **Secrets**: `JWT_SECRET` from a secret manager; prefer asymmetric keys via your IdP.
- **Scale-out**: replace in-memory repository, event bus and rate-limit counters
  (Postgres / Redis or NATS / Redis). See design §1 and §7.1.
- **Alerts** worth having, all from `/metrics`: `api_requests_total{outcome="internal"}`
  rate, p99 of `api_request_duration_seconds`, `webhook_deliveries_total{outcome="retry"}`
  ratio, `api_active_connections` saturation vs `WS_MAX_CONNECTIONS`.
- **Shutdown**: SIGTERM triggers drain; give pods `terminationGracePeriodSeconds` >= 30.
- **Webhooks**: add an egress proxy; run the dispatcher from a durable outbox.

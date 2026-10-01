# Production APIs — six API styles, one domain

REST · GraphQL · gRPC · WebSocket · Webhooks · SOAP, each built for a **real consumer need**
and hardened the way a production service would be: authentication and scopes, validation,
idempotency, typed errors, rate limiting, observability, graceful shutdown, secure defaults,
and acceptance tests traced to written scenarios.

> **Start here:** [docs/scenarios.md](docs/scenarios.md) (who needs what, and why each style)
> → [docs/design.md](docs/design.md) (how it is built, security model, trade-offs)
> → [docs/operations.md](docs/operations.md) (run, deploy, monitor).

## The six APIs at a glance

| # | Style | Consumer scenario | Port | Highlights |
|---|-------|-------------------|------|------------|
| S1 | **REST** | Partner checkout backend | 3001 | OpenAPI, `Idempotency-Key`, ETag / `If-Match`, cursor pagination, RFC 9457 errors, rate limiting |
| S2 | **GraphQL** | Mobile "My Orders" screen | 3002 | DataLoader (no N+1), depth limit, introspection off in prod, typed error codes, subscriptions |
| S3 | **gRPC** | Fulfillment & Payments services | 3003 | Versioned protobuf, auth interceptor, deadlines, server streaming, health service, load shedding |
| S4 | **WebSocket** | Live support wallboard | 3004 | Auth at handshake, browser-safe token, heartbeat, backpressure, frame/rate/connection limits, Origin allow-list |
| S5 | **Webhooks** | Accounting, carrier, Slack | 3005 | HMAC signatures, replay protection, retries + jitter, dead-letter, SSRF guard, reference receiver |
| S6 | **SOAP** | Legacy wholesale ERP | 3006 | Published WSDL, typed faults, XXE/entity-bomb safe, validated against an independent client (Zeep) |

All six sit on **one shared domain** (orders with a state machine, optimistic concurrency and
idempotent creation) so the only thing that differs is the protocol. Nothing about orders
lives in an adapter.

## Quick start

```bash
uv sync
uv run python scripts/demo.py     # one purchase touching all six APIs (journey J1)
uv run pytest                     # 59 acceptance + unit tests
uv run production-apis            # run all six servers
```

Journey J1 output:

```
[GraphQL]  Sam's app created order 95108301… status=PENDING total=24900c
[WebSocket] wallboard received order.created for 95108301…
[gRPC]     Payments set PAID (version 2); Fulfillment stream saw order.status_changed → ORDER_STATUS_PAID
[Webhooks] accounting processed 3 signed events in 4 requests (1 retried after 503)
[SOAP]     ERP GetOrder → status=SHIPPED
[REST]     partner poll → status=SHIPPED etag="3"; revalidation → 304
```

## Try each API

```bash
TOKEN=$(uv run python scripts/mint_token.py me)

# REST — idempotent create
curl -s localhost:3001/v1/orders -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H 'idempotency-key: demo-key-0001' \
  -d '{"customerId":"c1","items":[{"sku":"TENT","quantity":1,"unitPriceCents":24900}]}'

# GraphQL
curl -s localhost:3002/graphql -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"query":"{ orders { edges { node { id status customer { id orders { id } } } } } }"}'

# gRPC (grpcurl; reflection is intentionally off — use the proto)
grpcurl -plaintext -import-path proto -proto orders/v1/orders.proto -H "authorization: Bearer $TOKEN" \
  -d '{"page_size":5}' localhost:3003 orders.v1.OrderService/ListOrders

# WebSocket
uv run python -m websockets "ws://localhost:3004/ws" -H "Authorization: Bearer $TOKEN"   # then: {"type":"subscribe"}

# Webhooks — register an endpoint (the signing secret is returned exactly once)
curl -s localhost:3005/v1/webhook-endpoints -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"url":"https://partner.example/hooks","events":["order.created"]}'

# SOAP — WSDL for your ERP tooling
curl -s 'localhost:3006/soap?wsdl'
```

## What "production-level" means here

| Concern | Where |
|---|---|
| AuthN/Z: JWT (pinned alg, iss/aud/exp), per-operation scopes in every API | `core/auth.py` |
| One error model mapped to HTTP / gRPC / GraphQL / SOAP | `core/errors.py`, [design §4.1](docs/design.md) |
| Config validated at boot; prod refuses weak/missing secrets | `core/config.py` |
| Metrics (Prometheus) + structured logs with secret redaction | `core/metrics.py`, `core/logging.py` |
| Rate limits, body caps, security headers, request IDs | `core/http.py` |
| Injection / SSRF / replay / hijacking defences | [design §4.2](docs/design.md) |
| Graceful shutdown, health/readiness, bounded buffers, timeouts | [design §4.4](docs/design.md) |
| Container: multi-stage, non-root, read-only FS, dropped caps | `Dockerfile`, `docker-compose.yml` |
| CI: ruff, mypy (strict core), tests, stub drift check, image build | `.github/workflows/ci.yml` |
| Honest limitations (in-memory state, etc.) | [design §7.1](docs/design.md) |

## Testing

Every acceptance test is tagged with a scenario ID from [docs/scenarios.md](docs/scenarios.md):

```bash
uv run pytest -m "scenario" -q          # all acceptance tests
uv run pytest tests/test_webhooks.py    # one API
```

Real protocol stacks are exercised: a live gRPC server, real WebSocket connections, a real
HTTP receiver for webhooks, and Zeep (independent of our code) consuming the WSDL.

## Layout

```
docs/        scenarios, design, operations        proto/   gRPC contract       wsdl/   SOAP contract
src/production_apis/{core,domain,apis/{rest,graphql,grpc_api,websocket,webhooks,soap}}
tests/       one file per API + domain + journey  scripts/ demo, token, proto generation
```

MIT licensed.

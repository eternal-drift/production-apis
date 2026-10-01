# User Scenarios

Every API in this repo exists because a specific consumer has a specific problem that
style solves best. This document is the requirements source: each scenario has an ID
(`S1`…`S6`, plus the cross-API journey `J1`), acceptance criteria written as
Given/When/Then, and the non-functional requirements that drive the production details.
Tests are tagged with these IDs (`@pytest.mark.scenario("S1.2")`), so a criterion can be
traced to the code that proves it.

## The company

**Northwind Outfitters** is a mid-sized online retailer of outdoor gear. Its **Order
Service** is the system of record for customer orders. Orders move through a fixed
lifecycle:

```
PENDING ──► PAID ──► SHIPPED
   │          │
   └──────────┴──► CANCELLED
```

Northwind has many different consumers of that one service. No single API style serves
all of them well, which is exactly why there are six.

## Personas and API mapping

| ID | Consumer (persona) | Their problem | API style | Why this style |
|----|--------------------|---------------|-----------|----------------|
| S1 | **Checkout backend** (Priya, partner-integration engineer) | Place orders over flaky networks without double-charging; poll status cheaply | **REST** | Universal, cacheable, trivially retried with idempotency keys, works with every HTTP client and gateway |
| S2 | **Mobile app "My Orders" screen** (Dana, mobile developer) | One screen needs orders + items + customer history; each app version needs different fields; bandwidth is precious | **GraphQL** | Client chooses the shape; one round trip; no endpoint-per-screen; typed schema for codegen |
| S3 | **Fulfillment & Payments microservices** (Marcus, platform engineer) | Internal service-to-service calls at high volume with strict latency budgets and a contract that cannot silently drift | **gRPC** | Binary protobuf, HTTP/2 multiplexing, deadlines, generated clients, native server streaming |
| S4 | **Support wallboard** (Elena, ops lead) | Watch orders flow through the business live, filter to a customer during a call, no page refresh | **WebSocket** | Persistent bidirectional connection; client changes the filter without reconnecting |
| S5 | **Partner systems: accounting, carrier, Slack** (Tom, integrations manager) | Be told when something happens instead of polling every minute; must trust the sender; must survive their own outages | **Webhooks** | Server-initiated push to third parties; signed, retried, replay-protected |
| S6 | **Legacy ERP at a wholesale partner** (Greta, ERP consultant) | System only speaks SOAP/WSDL with a formal contract; cannot be changed | **SOAP** | Strict WSDL contract, typed faults, still the standard in banking/ERP/government integrations |
| J1 | **A single customer purchase** | One order touches every consumer above | all six | End-to-end journey (see below) |

---

## S1 — REST: partner checkout integration

**Persona.** Priya runs the checkout backend of a partner storefront that resells Northwind
products. Her service calls Northwind over the public internet.

**Goal.** Create an order and track it, with no duplicate orders when requests are retried.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S1.1 | Authenticated access | **Given** a request without a valid bearer token **When** it hits `/v1/orders` **Then** 401 `application/problem+json`. A read-only token creating an order gets 403. |
| S1.2 | Safe retries | **Given** Priya's network times out after sending `POST /v1/orders` with `Idempotency-Key: K` **When** she retries with the same key and body **Then** she receives the *same* order (200, `Idempotent-Replayed: true`), and only one order exists. Same key + different body → 409. |
| S1.3 | Actionable validation errors | **Given** a body with an empty items list **When** posted **Then** 400 problem+json naming the failing field, plus a `requestId` she can quote to support. |
| S1.4 | Cheap polling | **Given** Priya polls an order every few seconds **When** it has not changed **Then** `If-None-Match` yields 304 with no body. |
| S1.5 | Lost-update protection | **Given** two staff tools both try to change the same order **When** the second sends a stale `If-Match` **Then** 412 and no change. Illegal transitions (e.g. SHIPPED→PENDING) → 409. |
| S1.6 | Pagination at scale | **Given** thousands of orders **When** listing **Then** opaque cursor pagination (stable under inserts), `limit ≤ 100`. |
| S1.7 | Abuse protection | **Given** a client floods the API **When** it exceeds the rate limit **Then** 429, while health probes stay exempt. |

**Non-functional.** OpenAPI 3.1 contract generated from code; `/healthz`, `/readyz`,
`/metrics`; request-ID propagation; security headers; body-size limit; no stack traces in
errors.

## S2 — GraphQL: mobile "My Orders" screen

**Persona.** Dana builds the iOS/Android app. The order list shows id, status and total; the
detail screen adds line items; a "buy again" carousel needs the customer's other orders.

**Goal.** Render each screen with exactly one request returning exactly the fields it needs,
and see status updates live.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S2.1 | One round trip | **Given** the detail screen **When** Dana queries an order with `items` and `customer { orders }` **Then** one request returns the whole tree. |
| S2.2 | No N+1 | **Given** a list of 50 orders for the same customer **When** each resolves `customer.orders` **Then** the repository is hit once (batched DataLoader), not 50 times. |
| S2.3 | Typed, machine-readable errors | **Given** an illegal mutation **When** it fails **Then** `errors[0].extensions.code` is `CONFLICT`/`VALIDATION`/`FORBIDDEN`; internals are never leaked. |
| S2.4 | Hostile queries | **Given** a malicious deeply-nested query exploiting `Customer → orders → customer…` **When** executed **Then** it is rejected before execution (depth limit); introspection is off in production. |
| S2.5 | Live status | **Given** the app subscribes to `orderEvents` **When** an order changes **Then** the event arrives over the open stream (SSE/WebSocket). |
| S2.6 | Safe retries | `createOrder(input: {idempotencyKey})` is replay-safe, as in S1.2. |

## S3 — gRPC: fulfillment and payments services

**Persona.** Marcus owns the warehouse *Fulfillment* service and the *Payments* service.
Both call the Order Service thousands of times per second inside the cluster.

**Goal.** Fast, strongly-typed calls; the picker UI must be told the instant a paid order
needs shipping.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S3.1 | Typed contract | **Given** `orders.v1` protobuf **When** Marcus generates a client **Then** calls are compile-time checked; the proto follows additive-only evolution rules. |
| S3.2 | Payments marks an order paid | **Given** a PENDING order **When** Payments calls `UpdateOrderStatus(PAID, expected_version=1)` **Then** it succeeds; a stale version yields `FAILED_PRECONDITION`. |
| S3.3 | Deadlines | **Given** a caller with a 200 ms deadline **When** the server cannot answer in time **Then** it returns `DEADLINE_EXCEEDED` and stops working. |
| S3.4 | Error semantics | Domain errors map to canonical status codes (`NOT_FOUND`, `INVALID_ARGUMENT`, `PERMISSION_DENIED`, `UNAUTHENTICATED`). |
| S3.5 | Streaming | **Given** Fulfillment opens `WatchOrders` **When** orders are created/changed **Then** events stream down with flow control; cancellation frees server resources. |
| S3.6 | Orchestrator-friendly | Standard `grpc.health.v1` health service; graceful shutdown flips to `NOT_SERVING` before draining. |

## S4 — WebSocket: support wallboard

**Persona.** Elena's support team keeps a wallboard on a wall-mounted TV; agents also open
it during calls to follow one customer's order.

**Goal.** A live feed with no refreshing, narrowed on demand.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S4.1 | Authenticated handshake | **Given** a connection without a valid token **When** upgrading **Then** it is rejected (401) *before* any socket is opened. Browsers pass the token through `Sec-WebSocket-Protocol`. |
| S4.2 | Live feed | **Given** a connected client subscribed to everything **When** an order is created **Then** it receives the event within the same second. |
| S4.3 | Change the filter live | **Given** a call comes in **When** the agent sends `{"type":"subscribe","customerId":"C1"}` **Then** only that customer's events follow, with no reconnect. |
| S4.4 | Dead peers | **Given** a laptop lid is closed **When** pongs stop **Then** the server terminates the connection (heartbeat) and frees resources. |
| S4.5 | Protection | Oversized frames close the socket (1009); message-rate flooding closes it (1008); slow consumers are disconnected rather than buffered; connection cap enforced; Origin allow-list. |

## S5 — Webhooks: accounting, carrier and Slack

**Persona.** Tom's integrations team connects Northwind to a partner's accounting system, a
shipping carrier and an internal Slack channel. Those systems must learn about order
changes without polling.

**Goal.** Reliable, authenticated, at-least-once delivery that the receiver can trust and
de-duplicate.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S5.1 | Subscribe | **Given** Tom registers `{url, events}` **When** created **Then** the response returns a signing secret exactly once. |
| S5.2 | Authentic delivery | **Given** an order is created **When** delivered **Then** the request carries `Webhook-Id`, `Webhook-Timestamp` and an HMAC-SHA256 signature over `timestamp.body`; the receiver verifies it in constant time. |
| S5.3 | Replay protection | **Given** a captured delivery **When** replayed after 5 minutes, with a tampered body, or with a wrong secret **Then** verification fails. |
| S5.4 | Survives receiver outage | **Given** the receiver returns 503 twice **When** delivering **Then** retries use exponential backoff + jitter and eventually succeed with the *same* `Webhook-Id`; permanent 4xx failures do not retry; exhausted deliveries land in a dead-letter list. |
| S5.5 | Idempotent receiving | **Given** at-least-once delivery **When** the same `Webhook-Id` arrives twice **Then** the reference receiver processes it once. |
| S5.6 | SSRF protection | **Given** a malicious tenant registers `http://169.254.169.254/…` or `localhost` **When** creating the endpoint **Then** it is rejected (private/link-local/loopback blocked in production). |

## S6 — SOAP: legacy wholesale ERP

**Persona.** Greta consults for a wholesale partner whose 15-year-old ERP can only import a
WSDL and send SOAP envelopes. Nothing about it will change.

**Goal.** Place and track wholesale orders using the partner's tooling.

| # | Scenario | Given / When / Then |
|---|----------|---------------------|
| S6.1 | Published contract | **Given** the ERP tooling **When** it fetches `?wsdl` **Then** it gets a valid WSDL it can generate stubs from. |
| S6.2 | Typed operations | `CreateOrder`, `GetOrder`, `ListOrders`, `UpdateOrderStatus` work through a standard SOAP client (tested with Zeep). |
| S6.3 | Contractual faults | **Given** an unknown order **When** requested **Then** a SOAP 1.1 `Fault` (faultcode `soap:Client`) with a business `ErrorCode` in `detail` and HTTP 500, as the spec requires. |
| S6.4 | Safe XML | **Given** a hostile envelope with an external entity / billion-laughs payload **When** posted **Then** it is rejected, never expanded (XXE-safe parser). Oversized bodies rejected. |
| S6.5 | Authenticated | Same JWT as other APIs, carried in the HTTP `Authorization` header (the ERP's gateway injects it). |

---

## J1 — One purchase, six consumers

> Dana's customer **Sam** buys a tent in the mobile app.

1. **GraphQL** — the app calls `createOrder` (with an idempotency key); Sam's "My Orders" screen shows it **PENDING**.
2. **WebSocket** — Elena's wallboard flashes the new order instantly.
3. **gRPC** — Payments confirms the charge and calls `UpdateOrderStatus(PAID)`.
4. **gRPC stream** — Fulfillment's `WatchOrders` receives the PAID event; the picker prints a label and sets **SHIPPED**.
5. **Webhooks** — accounting receives a signed `order.status_changed`, and the Slack bot posts to #orders. The first delivery fails (503) and is retried.
6. **SOAP** — the wholesale ERP queries `GetOrder` to reconcile.
7. **REST** — Priya's partner storefront polls with `If-None-Match` and sees the SHIPPED state.

`scripts/demo.py` runs this journey against a live stack and prints each consumer's view;
`tests/test_journey.py` asserts it.

## Cross-cutting requirements (all scenarios)

| Concern | Requirement |
|---------|-------------|
| Authentication | JWT HS256 (algorithm pinned), issuer/audience checked, 5 s clock leeway |
| Authorization | Scopes `orders:read`, `orders:write` enforced on every operation in every API |
| Consistency of failure | One error taxonomy mapped to each protocol's native errors |
| Observability | Structured JSON logs with secret redaction; Prometheus metrics labelled by `api`/`operation`/`outcome` |
| Resilience | Health/readiness, graceful shutdown, bounded buffers, timeouts everywhere |
| Config | Validated at startup; production refuses to boot without a real secret |
| Evolution | REST `/v1`, protobuf additive-only, GraphQL deprecation, SOAP namespace versioning |

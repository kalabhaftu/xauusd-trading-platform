# Architecture

The CLI, trading service, research workers, and Streamlit dashboard share a local SQLite database using WAL,
foreign keys, FULL synchronization, short transactions, and separate SQLAlchemy connections. Alembic revision
0001 defines candles, typed order-intent state, commands, key/value durable state, and append-only audit tables.

Strategies never receive broker execution methods. They generate typed signals from completed candles. The
execution engine normalizes protective prices, reconciles account state, obtains a centralized risk decision,
persists intent, performs broker preflight, rechecks permission/quote/risk, and submits through the demo-only adapter.

Order states include PREPARED, SUBMITTING, FILLED, PARTIAL, REJECTED, ACCEPTED, UNKNOWN, EXPIRED, and resolved
partial/closure states. A PREPARED intent proves submission had not begun and is expired on recovery. SUBMITTING
or UNKNOWN proves only possible submission; broker deals/orders/positions must resolve it before new entries.
MT5 comments may be rewritten, so recorded deal identifiers are also used. Unprovable absence stays blocked.

Dashboard setting and position commands are validated again by the engine. Emergency halt records its durable
state immediately and enqueues the corresponding control event. Halt/reset does not close protected positions;
missing stops on bot-owned positions trigger a bounded closure attempt. External positions are never closed.

The account-scoped Windows Global named mutex prevents service competition across Windows sessions and working
directories. Research can run without MT5. CLI download and arming take the same mutex to avoid changing the
terminal account while a service is active. Use one trusted local dashboard instance; SQLite is not a multi-host service.

An outbox notification thread sends optional Telegram messages independently, with a five-attempt limit. Failed
delivery is persisted; notification failures cannot bypass risk controls or delay broker submission. Secrets are
excluded from dashboard settings, audit payloads, and notification text, with structured-log redaction as a second layer.

LIVE is recognized only to produce a clear unsupported-mode error. A future live implementation requires a separate
credential set, identity, explicit enable flag, fresh authorization, additional integration tests, and deployment review.

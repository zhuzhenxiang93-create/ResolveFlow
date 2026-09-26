# ResolveFlow frontend

Vue 3 + Vite portfolio interface for `/chat` and `/commerce/*`.

```bash
npm ci
VITE_DEMO_MODE=true npm run dev
npm run build
```

Vite proxies `/api/python` to `http://localhost:8000`. The isolated demo backend
must be started separately; see the [root Quick Start](../README.md).
Set `VITE_DEMO_MODE=true` at build time for automatic session provisioning and
Reset Demo. Production mode uses existing JWTs through collapsed Advanced settings.
Demo and production settings have separate browser storage keys.

- `App.vue`: product framing, scenarios, conversation and view selection.
- `CommercePanel.vue`: API-backed purchases, cases, preferences and review queue.
- `ActionCard.vue`: quote, impact, state timeline and explicit decisions.
- `AdvancedSettings.vue`: connection, JWT, knowledge and monitoring tools.
- `lib/backends.js`: API adapters; historical adapters remain unused by current UI.

Reviewer controls use reviewer JWTs; user decisions use user JWTs. No local
eligibility calculation or optimistic successful state. Completed case recovery
uses `/commerce/cases`. Reset creates fresh identities and clears conversation UI.
Sources are expandable chips. Raw response metadata lives in Developer details.

The offline banner is intentional: simulated execution, deterministic language
rules and lexical policy retrieval must not be presented as a live LLM run.
For Nginx/Docker, preserve the `/api/python` proxy to the separately configured API.
No tokens or signing keys should be put into Vite environment variables.

[Validation and browser gaps](../docs/validation.md).

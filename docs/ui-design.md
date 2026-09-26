# Recruiting demo information architecture

Baseline: main 42ccdc1. Inspected App.vue, CommercePanel.vue, styles.css,
backends.js, api/main.py, commerce_routes.py, auth.py, CommerceStore,
CommerceConversation, ConversationService, README and acceptance scripts.

| Capability | Placement |
|---|---|
| Chat, citations, scenario entry points | Main conversation |
| Goods, subscriptions, shipments, invoices, payment and refund records | Business sidebar; readable expanded details |
| Object selection | Above action cards; explicit user choice |
| Confirmation and action timeline | Main business panel |
| Independent review and simulated fulfilment | Reviewer view |
| Response preferences / clear memory | Compact collapsed preferences |
| Route, intent, sources, raw response | Developer details, collapsed |
| JWT, endpoint, knowledge import/search, monitor | Advanced settings, collapsed |
| Social profile advertising | Removed |
| Retired ActionRuntime controls | Removed from active interface; historical backend remains |

Use a neutral light canvas, dark teal accent, restrained borders and readable
amounts. Desktop: chat and business context in two columns; stack below 850px.
Actions use persisted decisions and quote flags to show only relevant steps.
No browser-calculated eligibility, amount, or synthetic successful transitions.

Demo provisioning must run in a separate demo-only application and isolated
session ledger. Production token issuance remains unchanged. Reset starts a new
isolated session rather than deleting transactional audit. No admin token is
issued by the demo bootstrap.

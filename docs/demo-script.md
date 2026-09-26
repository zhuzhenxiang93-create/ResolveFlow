# Recruiting demo walkthrough

Start the isolated backend and Vite as described in the root README. Reset Demo.
No manual token entry is needed. All refunds and fulfilment are simulated.
The local acceptance used Qwen Plus; the banner states the active mode.

## 30 seconds: what it does

“ResolveFlow answers policies, queries your purchases, and executes controlled
after-sales requests. The model interprets language; CommerceStore determines
amounts and eligibility. Users confirm, and a separate identity reviews refunds.”
Show the three value statements, scenario buttons, conversation and purchases.

## 2–3 minutes: why the controls matter

1. Click **Refund policy**. Open a source chip: policy origin and document ID are
   visible. No refund Case is created. Retrieval in this isolated Demo is lexical.
2. Click **Choose a purchase**. Eight candidates appear. “Ambiguity cannot authorize
   an arbitrary refund.” Select **无线耳机**.
3. Show **¥259**, the return requirement and invoice impact. Click **Confirm refund**.
4. Switch to **Reviewer view**. “This uses another authenticated identity.” Click
   **Approve**, then **Simulate return received**, then **Simulate payment receipt**.
5. Switch to **User view** and show **Completed**. Refresh: the Case returns. Expand
   purchase details to show a single simulated refund entry. Chat itself is not
   restored on reload.

## Up to 5 minutes: complete the product story

6. Click **Cancel renewal**. Confirm: Basic auto-renew becomes OFF, current benefits
   remain and no refund is created.
7. Click **Billing + technical**. The Pro duplicate payment has a **¥99** quote,
   while the separate Technical Agent explains 401. The refund still awaits consent.
   Open Developer details only if asked: actual `lookup_error_code` execution and
   `native_llm` interpretation are recorded. Never describe technical guidance as
   fixing a real account, or claim the Demo uses hybrid RAG.
8. **Reset Demo** shows a new identity with eight fresh purchases and no prior
   Cases. Prior audit databases are preserved.

If the banner says **Offline rules**, scenes 1–4 still work; scene 5 explicitly
states that Technical Agent is unavailable. Do not use that as a live-model demo.

Evidence: [acceptance report](validation.md), [screenshots](screenshots/README.md),
[real-provider checks](live-validation.json).

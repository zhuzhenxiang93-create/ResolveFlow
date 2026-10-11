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
   visible. No refund Case is created. In Dify mode, General Agent calls the Dify knowledge tool. In direct/offline mode, retrieval remains lexical.
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
   In Dify mode, show the separate **Technical Agent · Live model** message. Supervisor dispatches Commerce and Technical; the quote still comes from the Python business backend. Allow roughly a minute for native Agent execution.
   Open Developer details only if asked to inspect the returned plan and tool traces. Never describe technical guidance as
   fixing a real account, or claim the Demo uses hybrid RAG.
8. **Reset Demo** shows a new identity with eight fresh purchases and no prior
   Cases. Prior audit databases are preserved.

9. Optional (1 min): open the **Evaluation** tab (`#evaluation`). Show the four
   top tiles, then switch Bad cases to **Baseline · 32**. Click a disambiguation
   case: the baseline auto-picked one of two identical headphone orders. Close
   with the holdout card (2/16 → 15/16), explaining that the main set was used for
   fixing, so the holdout is the honest generalisation check. Everything comes from
   the generated JSON report; nothing runs on page load.

If the banner says **Offline rules**, scenes 1–4 still work; scene 5 explicitly
states that Technical Agent is unavailable. Do not use that as a live-model demo.

Evidence: [acceptance report](validation.md), [screenshots](screenshots/README.md),
[real-provider checks](live-validation.json).

## Dify-specific extension

Use the published multi-Agent workflow and launch script in [dify/README.md](../dify/README.md). Show the five native Agent nodes and explain that each has an independent task and read-only tools. They execute only when dispatched, currently in sequence.

Try “请比对账单：应扣99元，实际扣198元。另外我要转人工。” Billing should show the user-provided 99元 difference; Escalation should show a handoff summary and state that no human has been connected. Neither result grants refund eligibility or authority.

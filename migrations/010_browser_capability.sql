-- 010 — give the assistants that already exist a browser (phase 31).
--
-- `run_qa` used to mean "a Tester runs a QA pass". It now means something bigger and more
-- basic: **this assistant may look at the page in a real browser**. That is the capability
-- behind `mikeweb` in the beat container and `/api/assistant/browser/*` on the control plane.
--
-- The Developer template now ships it, because the incident that prompted all of this was a
-- Developer explaining a broken site from deployment records alone: health green, every step
-- clean, therefore "an SSL error at the platform ingress layer". The real cause was a CSP
-- header it had added itself, blocking the preview iframe. It could not see what the user
-- saw, so it produced a confident, wrong answer. An engineer that ships and cannot look is
-- the whole failure mode.
--
-- A template change only affects assistants created AFTER it, so the ones already running
-- would have kept shipping blind. This grants the browser to every existing assistant that
-- can write code — the population the incident is about.
--
-- Note what this deliberately does NOT do: it does not hand the browser to every assistant.
-- An Expense-management assistant has no business driving Chrome, and capabilities are the
-- thing the runtime actually enforces, so widening them is never a default.
--
-- House rules: idempotent (re-running changes nothing), parameterless, no reserved words.

UPDATE builderapps.assistants
   SET capabilities = capabilities || '["run_qa"]'::jsonb
 WHERE capabilities @> '["edit_code"]'::jsonb
   AND NOT (capabilities @> '["run_qa"]'::jsonb);

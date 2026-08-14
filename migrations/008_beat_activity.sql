-- 008 — what the assistant is doing RIGHT NOW, durably (phase 30).
--
-- An assistant that works for two minutes and shows nothing until it finishes reads as a
-- frozen UI, and that is the single biggest reason the assistants "feel dead". So the beat
-- container streams what its coding agent actually does — every tool call Pi makes, every
-- file it touches, the commit, the deploy, the health gate — and each line lands here as it
-- happens.
--
-- Why a column on the beat rather than the chat thread: the chat thread is REPLACED
-- wholesale by the SPA (`PUT /api/projects/{id}/messages` sanitises to {role,text} and
-- overwrites), so anything the server appended mid-beat would be clobbered by the next
-- client save. The beat row is server-owned, already the assistant's audit log, and already
-- restored on reload — which is exactly the durability the activity feed needs, for free.
--
-- Append-only and capped in code (`assistants.append_activity`), so a runaway agent cannot
-- grow one row without bound.
--
-- Shape: [{"ts": "...", "kind": "phase|tool|text|result", "icon": "✎", "text": "...",
--          "detail": "...", "ok": true}]
--
-- House rules: idempotent, no reserved keyword as a column name.

ALTER TABLE builderapps.assistant_beats
    ADD COLUMN IF NOT EXISTS activity jsonb NOT NULL DEFAULT '[]'::jsonb;

-- "show me every beat with activity for this project, newest first" — the restore query the
-- /builder left pane runs on load.
CREATE INDEX IF NOT EXISTS assistant_beats_activity_idx
    ON builderapps.assistant_beats (project_id, ts DESC)
    WHERE jsonb_array_length(activity) > 0;

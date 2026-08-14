-- 004 — give every project an EXPLICIT private subnet (phase 28, found the hard way).
--
-- Docker only has ~31 default-pool networks (172.17.0.0/12 as /16s + 192.168.0.0/16 as /20s).
-- With ~20 apps deployed, 242 hit `all predefined address pools have been fully subnetted`
-- and EVERY new build died at deploy_skeleton — the platform simply could not create a 21st
-- app. Raising it in /etc/docker/daemon.json needs a dockerd restart, which would bounce all
-- ~110 unrelated production containers on that box.
--
-- So we stop asking Docker to choose: each project gets its own /24 out of 10.0.0.0/8 (which
-- is completely unused on 242), written into the compose as an explicit ipam config. Docker
-- never touches its default pools, and the ceiling goes from ~20 apps to ~40 000.
--
-- net_index is allocated once from a sequence and then pinned to the project, so a redeploy
-- always reproduces the same subnet.
-- Idempotent (house rule).

CREATE SEQUENCE IF NOT EXISTS builderapps.project_net_seq AS integer START WITH 1;

ALTER TABLE builderapps.projects
    ADD COLUMN IF NOT EXISTS net_index integer;

CREATE UNIQUE INDEX IF NOT EXISTS projects_net_index_key
    ON builderapps.projects (net_index) WHERE net_index IS NOT NULL;

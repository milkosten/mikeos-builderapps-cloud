# mikeos-assistant-runtime — the workbench ONE assistant beat runs inside.
#
# Mike's constraint, and the right one: "an assistant that can only call an LLM is a chatbot;
# an assistant with a checkout, a shell and tools is a colleague." So this image ships real
# CLI tools, the beat program clones the project repo, and the actual code editing is done by
# **Pi** — an open-source coding agent — running right here with the checkout in front of it.
#
# WE DO NOT WRITE OUR OWN CODING LOOP. `@earendil-works/pi-coding-agent` is pinned below and
# driven non-interactively by beat.py. (Note the scope: Pi is the ASSISTANT's coding engine.
# The build pipeline that produces v1 of an app is a different, strict, ordered thing and is
# untouched by this — assistants are disconnected from it.)
#
# Ephemeral: the scheduler starts one of these per beat with `--rm`. It clones/refreshes,
# perceives, reasons, codes, commits, asks for a deploy, writes its beat record and exits.
# Nothing survives except /workspace (the assistant's own cached checkout, bind-mounted).
#
# WHAT IS DELIBERATELY ABSENT:
#   * the docker CLI and the docker socket — an assistant never deploys anything itself, it
#     asks the control plane, which runs the normalizer + health gate + rollback. Docker
#     socket access is host root.
#   * any model provider credential. Pi is pointed at the control plane's OpenAI-compatible
#     proxy and authenticates with the per-assistant token it already holds; the real
#     OpenRouter key never enters this container. See server/llm_proxy.py.

# Pi 0.84.x requires Node >= 22.19 (it crashes on Node 20 inside undici). node:24 is what
# Pi's own image uses. The `legacy-node20` dist-tag (0.74.2) exists if this ever has to run
# on an older runtime — but then pin THAT version, don't mix.
FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The starting kit (deliberately extensible — Mike's call what else belongs here):
#   git            the checkout, the log, the diff, the commit, the push
#   node/npm       from the base image; the generated apps are node and Pi is a node CLI
#   python3        the beat program + general scripting
#   ripgrep        searching a repo without slurping it into memory (house rule), and Pi's
#                  own grep tool shells out to it
#   jq curl        JSON + HTTP from the shell
#   gawk sed less  text surgery
#   postgresql-client / redis-tools   the project's own datastores
#   ca-certificates  TLS to Gitea and the control plane
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git curl jq ripgrep gawk sed less \
      python3 python3-minimal \
      postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*

# THE CODING AGENT. Pinned exactly: an agent that silently changes version under an
# unattended fleet is an outage waiting for a quiet weekend.
# --ignore-scripts: nothing in this image should run a package's install hooks.
ARG PI_VERSION=0.84.2
RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force \
    && pi --version

# PI_OFFLINE kills every non-inference network call Pi would otherwise make at startup (its
# version check, its install telemetry, and the remote model-catalog refresh). This container's
# egress should reach the control plane and Gitea and nothing else, so those calls would just
# hang; and the model we use is defined locally in models.json anyway.
ENV PI_OFFLINE=1 \
    PI_TELEMETRY=0 \
    PI_CODING_AGENT_DIR=/tmp/pi/agent

# Non-root by construction. The scheduler also passes --user, but an image that only works
# as root is one --user flag away from a bad surprise.
RUN useradd -r -u 10001 -m -d /home/assistant -s /bin/bash assistant \
    && mkdir -p /workspace && chown 10001:10001 /workspace

COPY beat.py /app/beat.py
RUN chmod 0555 /app/beat.py

# THE BROWSER. `curl` in this image proves a process answered; `mikeweb` proves the page
# actually works — it drives a real headless Chrome and reports rendered text, JS console
# errors and failed requests. Deliberately in addition to curl, never instead of it.
#
# NO CREDENTIAL COMES WITH IT. chrome-pool's shared Basic auth stays on the control plane
# (`server/browser_proxy.py`); this CLI authenticates with the same per-assistant `asst_…`
# token the container already holds, exactly like Pi does for the model. So an agent gets a
# browser without ever holding the key to the estate's browser fleet.
COPY mikeweb /usr/local/bin/mikeweb
RUN chmod 0555 /usr/local/bin/mikeweb

# THE SHARED WORKSPACE (phase 32). `ws` is the work-tracker every assistant on this project,
# the build pipeline and the human all write to: features, bugs, tasks, test cases, docs and
# knowledge-base notes, each with a status, comments and a who-did-what trail. A beat
# container is deleted when the beat ends, so this is the ONLY memory an assistant has that
# outlives it — and the only way two assistants can hand work to each other.
#
# SAME CREDENTIAL RULE AS mikeweb, and it matters more here: NO KEY IS BAKED IN. The
# project's `workspace-api-key` is injected per beat by the scheduler as WORKSPACE_API_KEY
# (server/assistant_runtime.py) and is scoped to exactly one project. An image is pulled,
# copied and shared; a key inside one is a key in everyone's hands. Verify with:
#   docker run --rm --entrypoint sh mikeos-assistant-runtime -c 'env'
COPY ws /usr/local/bin/ws
RUN chmod 0555 /usr/local/bin/ws

# TALKING TO COLLEAGUES (phase 33). `ws` is the noticeboard; `msg` is tapping one assistant on
# the shoulder — and because containers are ephemeral, "tapping on the shoulder" means the
# control plane stores the message and STARTS A BEAT for the recipient. So this one CLI can
# spend the project's money, and every bound that stops it running away (chain depth,
# coalesced wakes, the daily budget) is enforced server-side in `server/messaging.py`, not
# here: a tool in an LLM-driven container is a suggestion, not a control.
#
# Same credential rule as mikeweb and ws: nothing baked in. It authenticates with the
# per-assistant `asst_…` token already in the environment, which is also what makes a forged
# sender impossible — the FROM of a message is the token, never the request body.
COPY msg /usr/local/bin/msg
RUN chmod 0555 /usr/local/bin/msg

# The skills that tell the coding agent these tools exist and when to reach for them. Pi's
# own mechanism (the Agent Skills spec: a directory with SKILL.md + frontmatter), loaded by
# beat.py with an explicit `--skill` per directory. They live in the IMAGE, not in the repo:
# `--no-skills` still disables discovery from the checkout, so an LLM-written repository
# cannot introduce a skill that configures the agent editing it next.
COPY skills /app/skills
RUN chmod -R a=rX /app/skills

# git refuses to operate in a directory it does not consider owned by the caller; the
# bind-mounted /workspace is chowned to 10001 by the control plane, but make it explicit so a
# host with a different mount uid degrades to a clear message rather than a cryptic git error.
RUN git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory '*'

USER 10001:10001
WORKDIR /workspace

# One beat, then exit. The container IS the beat.
ENTRYPOINT ["python3", "/app/beat.py"]

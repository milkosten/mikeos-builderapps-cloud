# mikeos-assistant-runtime — the workbench ONE assistant beat runs inside.
#
# Mike's constraint, and the right one: "an assistant that can only call an LLM is a chatbot;
# an assistant with a checkout, a shell and tools is a colleague." So this image ships real
# CLI tools and the beat program clones the project repo and actually uses them.
#
# Ephemeral: the scheduler starts one of these per beat with `--rm`. It clones/refreshes,
# perceives, reasons, acts, writes its beat record and exits. Nothing survives except
# /workspace (the assistant's own cached checkout, bind-mounted from the host).
#
# WHAT IS DELIBERATELY ABSENT: the docker CLI and the docker socket. An assistant never
# deploys anything itself — it asks the control plane, which runs the normalizer + health
# gate + rollback. Docker-socket access is host root.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The starting kit (deliberately extensible — Mike's call what else belongs here):
#   git            the checkout, the log, the diff, the commit, the push
#   nodejs npm     the generated apps are node; the assistant must be able to lint/run them
#   python3        the beat program + general scripting
#   ripgrep        searching a repo without slurping it into memory (house rule)
#   jq curl        JSON + HTTP from the shell
#   gawk sed       text surgery
#   postgresql-client / redis-tools   the project's own datastores
#   ca-certificates  TLS to Gitea and the control plane
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git curl jq ripgrep gawk sed less \
      python3 python3-minimal \
      nodejs npm \
      postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*

# Non-root by construction. The scheduler also passes --user, but an image that only works
# as root is one --user flag away from a bad surprise.
RUN useradd -r -u 10001 -m -d /home/assistant -s /bin/bash assistant \
    && mkdir -p /workspace && chown 10001:10001 /workspace

COPY beat.py /app/beat.py
RUN chmod 0555 /app/beat.py

# git refuses to operate in a directory it does not consider owned by the caller; the
# bind-mounted /workspace is chowned to 10001 by the control plane, but make it explicit so a
# host with a different mount uid degrades to a clear message rather than a cryptic git error.
RUN git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory '*'

USER 10001:10001
WORKDIR /workspace

# One beat, then exit. The container IS the beat.
ENTRYPOINT ["python3", "/app/beat.py"]

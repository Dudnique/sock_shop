# The triage bot: a standard-library-only Python service.
#
# No `pip install`, no requirements.txt, no base image with a toolchain: the
# whole component is the standard library, which keeps the supply chain tiny and
# makes the build reproducible. It also matches the guardrail: there is no
# Kubernetes client here to import, because the bot has no cluster credentials.
FROM python:3.12-slim

# Unbuffered stdout: logs appear in `kubectl logs` as they happen, which is what
# makes a live demo readable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only the package is copied: tests and docs stay out of the runtime image.
COPY triage_bot ./triage_bot

# M1 runs as an unprivileged user with a read-only root filesystem (see the
# Deployment): the only writable path is the emptyDir mounted at /data.
USER 65534:65534

EXPOSE 8080

ENTRYPOINT ["python", "-m", "triage_bot"]

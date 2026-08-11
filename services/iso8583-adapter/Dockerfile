# iso8583-adapter
#
# Build context is the REPOSITORY ROOT, not this directory:
#     docker build -f services/iso8583-adapter/Dockerfile .
# That is what lets the image install libs/mfcommon as a real package rather
# than reaching it through a sys.path hack at runtime.

# ---- build stage: resolve dependencies, nothing else ----
FROM python:3.12-slim AS builder
WORKDIR /build

COPY services/iso8583-adapter/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# mfcommon installed as a package, so `import mfcommon` works the same in the
# container as it does in a local venv. Copied after requirements so a change
# to shared code does not invalidate the dependency layer.
COPY libs/mfcommon /build/libs/mfcommon
RUN pip install --no-cache-dir --prefix=/install --no-deps /build/libs/mfcommon

# ---- runtime stage: clean image, no build tools, no pip cache ----
FROM python:3.12-slim
WORKDIR /app

# Installed system-wide rather than into a named user's home directory.
# This matters specifically for OpenShift: it runs containers under a
# RANDOMLY assigned UID, not necessarily any user defined in this image, so
# anything tied to a fixed user's home can end up unreadable to whatever UID
# actually runs, which surfaces as a confusing "executable file not found
# in $PATH" rather than as a permission error.
COPY --from=builder /install /usr/local

COPY services/iso8583-adapter/app /app/app

# OpenShift's random runtime UID is always a member of GID 0. Granting GID 0
# the owner's permissions is the documented pattern for supporting that model.
RUN chgrp -R 0 /app && chmod -R g=u /app

# A concrete non-root UID. Honoured by plain Docker for defence in depth;
# OpenShift overrides it with its own random UID, which is exactly what the
# GID 0 permissions above exist for.
USER 1001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8085

# Hits the real /health endpoint rather than merely checking the process is
# alive. Note this is the LIVENESS check, /ready is the one that verifies
# downstream dependencies, and OpenShift probes it separately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8085/health')" || exit 1

# No secrets baked in. JWT_SECRET, REDIS_URL, database DSNs and
# HSM_MASTER_KEY_HEX all arrive from the environment.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]

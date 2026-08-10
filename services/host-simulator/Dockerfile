# host-simulator
#
# Build context is the REPOSITORY ROOT, not this directory:
#     docker build -f services/host-simulator/Dockerfile .
# That is what lets the image install libs/mfcommon as a real package rather
# than reaching it through a sys.path hack at runtime.

# ---- build stage: resolve dependencies, nothing else ----
FROM python:3.12-slim AS builder
WORKDIR /build

COPY services/host-simulator/requirements.txt ./requirements.txt
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
# actually runs -- which surfaces as a confusing "executable file not found
# in $PATH" rather than as a permission error.
COPY --from=builder /install /usr/local

COPY services/host-simulator/app /app/app

# OpenShift's random runtime UID is always a member of GID 0. Granting GID 0
# the owner's permissions is the documented pattern for supporting that model.
RUN chgrp -R 0 /app && chmod -R g=u /app

# A concrete non-root UID. Honoured by plain Docker for defence in depth;
# OpenShift overrides it with its own random UID, which is exactly what the
# GID 0 permissions above exist for.
USER 1001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 9999

# No HTTP health endpoint -- this speaks raw TCP. The check opens a socket,
# which is the only meaningful liveness signal for a listener.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket;socket.create_connection(('127.0.0.1',9999),timeout=3).close()" || exit 1

CMD ["python", "-m", "app.main"]

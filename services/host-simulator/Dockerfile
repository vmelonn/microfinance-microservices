# host-simulator
#
# Builds FROM microfinance-base, which already carries fastapi, uvicorn,
# pydantic, httpx and mfcommon. Only this service's own extras are installed
# here, so a rebuild is seconds rather than minutes.
#
# Build context is the REPOSITORY ROOT:
#     docker build -f services/host-simulator/Dockerfile .
#
# The base must exist first:
#     make build-base
#
# In the cluster the FROM line is satisfied by the microfinance-base
# ImageStream, because the BuildConfig sets strategy.dockerStrategy.from.
# Nothing pulls this name from a public registry.
FROM microfinance-base:latest

WORKDIR /app

# No extra packages: the base covers everything this service imports.

COPY services/host-simulator/app /app/app

# OpenShift assigns a random runtime UID that is always a member of GID 0, so
# granting GID 0 the owner's permissions is what makes the image work under
# that model.
RUN chgrp -R 0 /app && chmod -R g=u /app

# A concrete non-root UID. Honoured by plain Docker for defence in depth;
# OpenShift overrides it with its own, which is what the GID 0 permissions
# above are for.
USER 1001

EXPOSE 9999

# No HTTP endpoint: this speaks raw TCP. Opening a socket is the only
# meaningful liveness signal for a listener.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket;socket.create_connection(('127.0.0.1',9999),timeout=3).close()" || exit 1

CMD ["python", "-m", "app.main"]

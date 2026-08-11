# ace-stub
#
# Builds FROM microfinance-base, which already carries fastapi, uvicorn,
# pydantic, httpx and mfcommon. Only this service's own extras are installed
# here, so a rebuild is seconds rather than minutes.
#
# Build context is the REPOSITORY ROOT:
#     docker build -f services/ace-stub/Dockerfile .
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

COPY services/ace-stub/app /app/app

# Served verbatim at /Iso8583Gateway?wsdl, so the stub and a real ACE
# integration server publish the identical contract.
COPY ace/Iso8583Library/wsdl /app/wsdl

# OpenShift assigns a random runtime UID that is always a member of GID 0, so
# granting GID 0 the owner's permissions is what makes the image work under
# that model.
RUN chgrp -R 0 /app && chmod -R g=u /app

# A concrete non-root UID. Honoured by plain Docker for defence in depth;
# OpenShift overrides it with its own, which is what the GID 0 permissions
# above are for.
USER 1001

EXPOSE 8090

# Hits the real /health endpoint rather than merely checking the process is
# alive. This is LIVENESS; /ready is the one that verifies downstreams, and
# OpenShift probes it separately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8090/health')" || exit 1

# No secrets baked in. JWT_SECRET, REDIS_URL, database DSNs and
# HSM_MASTER_KEY_HEX all arrive from the environment.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]

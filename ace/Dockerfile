# IBM App Connect Enterprise, ISO 8583 SOAP gateway.
#
# NEVER BUILT. The base image lives behind IBM's entitled registry and the
# key had not come through. The build steps are correct as far as they can be
# verified without pulling the image; treat this as ready-to-try, not as
# known-good.
#
# Authenticate first:
#     docker login cp.icr.io --username cp --password <ENTITLEMENT_KEY>

FROM cp.icr.io/cp/appc/ace-server-prod:13.0.1.0-r1

# Accepting the licence is mandatory and the container refuses to start
# without it. The failure message does mention LICENSE, which is more than
# can be said for most startup failures in this stack.
ENV LICENSE=accept \
    ACE_SERVER_NAME=MFSERVER

USER root

# The compiled BAR. Produced by:
#     ibmint package --input-path . --output-bar-file Iso8583Gateway.bar \
#                    --project Iso8583Gateway --project Iso8583Library
COPY Iso8583Gateway.bar /home/aceuser/bars/

# server.conf.yaml carries the switch endpoint, timeouts, and log level.
COPY server.conf.yaml /home/aceuser/ace-server/overrides/

# OpenShift assigns a RANDOM UID at runtime, not the image's declared user.
# That UID is always a member of GID 0, so granting GID 0 the owner's
# permissions is the documented way to make the image work under that model.
# ACE is particularly sensitive here: the integration server writes its work
# directory at startup, and a permission failure surfaces as a generic
# "unable to create work directory" rather than anything mentioning UIDs.
RUN chgrp -R 0 /home/aceuser && \
    chmod -R g=u /home/aceuser

USER 1000

# 7800 = SOAP + admin REST, 7600 = web UI.
EXPOSE 7800 7600

# The integration server's own admin REST endpoint. Reports whether the
# deployed flows are actually running, not merely that the process started,
# ACE will happily run with a BAR that failed to deploy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:7600/apiv2/servers || exit 1

CMD ["ace_docker_run.sh"]

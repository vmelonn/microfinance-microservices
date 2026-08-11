# microfinance-base: the layer every service starts from.
#
# WHY THIS EXISTS. Eight of the nine services install an identical
# fastapi/uvicorn/pydantic/httpx stack from PyPI, and OpenShift builds share
# no layer cache between BuildConfigs. So every pipeline run downloaded and
# compiled the same packages nine times, concurrently, and each build took
# minutes. uvicorn[standard] alone pulls uvloop, httptools, watchfiles and
# websockets.
#
#   before:  9 builds x full install
#   after:   1 build  x full install, then 9 x (their own extras only)
#
# Once the base is stable it is not rebuilt, so subsequent runs skip the slow
# part entirely.
#
# Build context is the REPOSITORY ROOT:
#     docker build -f services/_base/Dockerfile -t microfinance-base:latest .
#
# NO USER AND NO CMD, deliberately. This image is never run. Setting USER 1001
# here would force every child to switch back to root to pip install its
# extras and then switch again, so the security posture is applied once, in
# each service's own Dockerfile, where it is visible next to what it protects.

FROM python:3.12-slim AS builder
WORKDIR /build

# Dependencies first, mfcommon second. mfcommon changes far more often than
# the pinned third-party stack does, and this ordering means editing shared
# code does not invalidate the expensive layer.
COPY services/_base/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY libs/mfcommon /build/libs/mfcommon
RUN pip install --no-cache-dir --prefix=/install --no-deps /build/libs/mfcommon

# ---- runtime layer: no build tools, no pip cache ----
FROM python:3.12-slim
WORKDIR /app

# Installed system-wide rather than into a named user's home directory. This
# matters for OpenShift specifically: it runs containers as a RANDOM UID, so
# anything under a fixed user's home can be unreadable to whatever UID
# actually runs. That surfaces as "executable file not found in $PATH", which
# is not a permission error and sends you looking in the wrong place.
COPY --from=builder /install /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

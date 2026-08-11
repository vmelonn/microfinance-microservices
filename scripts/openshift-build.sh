#!/usr/bin/env bash
#
# Build the images inside the OpenShift cluster.
#
#     bash scripts/openshift-build.sh                     # all nine, from Git
#     bash scripts/openshift-build.sh api-gateway         # just one
#     bash scripts/openshift-build.sh --local api-gateway # from the working tree
#
# DEFAULT: the cluster clones the GitHub repo at the ref in
# openshift/build/build.yaml and builds from it. Every resulting image is
# traceable to a commit.
#
# --local: uploads YOUR WORKING TREE instead and builds that, ignoring Git for
# this run. Useful while iterating on something uncommitted. The resulting
# image corresponds to no commit, so never use it for anything you need to
# reproduce, and note it will happily ship your uncommitted debugging.
#
# Requires: oc, and an active login. Does NOT require a local container engine.

set -euo pipefail

SERVICES=(
  api-gateway
  auth-service
  transaction-service
  risk-service
  ledger-service
  iso8583-adapter
  ace-stub
  host-simulator
  analytics-sync
)

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the 'oc' CLI is not on PATH."
  echo "  Download it from the cluster's own help menu (? -> Command line tools),"
  echo "  which guarantees a version matching your cluster."
  exit 1
fi

if ! oc whoami >/dev/null 2>&1; then
  echo "ERROR: not logged in. Run the 'oc login --token=... --server=...' command"
  echo "  from the OpenShift console (your name -> Copy login command)."
  exit 1
fi

NAMESPACE="$(oc project -q)"
echo "cluster:   $(oc whoami --show-server)"
echo "namespace: ${NAMESPACE}"
echo "user:      $(oc whoami)"
echo

FROM_LOCAL=0
TARGETS=()
for arg in "$@"; do
  case "$arg" in
    --local) FROM_LOCAL=1 ;;
    -*)      echo "unknown flag: $arg"; exit 1 ;;
    *)       TARGETS+=("$arg") ;;
  esac
done

if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=("${SERVICES[@]}")
  BUILD_BASE=1        # a full run rebuilds the base first
else
  BUILD_BASE=0        # a targeted rebuild assumes the base is current
fi

if [ "$FROM_LOCAL" -eq 1 ]; then
  echo "source:    LOCAL WORKING TREE (not Git, these images match no commit)"
  if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "           note: you have uncommitted changes, and they WILL be built"
  fi
else
  echo "source:    Git, per the BuildConfig"
fi
echo

# Every service Dockerfile starts FROM microfinance-base, so it has to be
# current before any of them build. It is the only build that compiles
# anything (uvloop, httptools), which is precisely why it is shared.
if [ "$BUILD_BASE" -eq 1 ]; then
  echo "=== microfinance-base ==="
  if [ "$FROM_LOCAL" -eq 1 ]; then
    oc start-build microfinance-base --from-dir=. --follow --wait
  else
    oc start-build microfinance-base --follow --wait
  fi
  echo
fi

for svc in "${TARGETS[@]}"; do
  echo "=== ${svc} ==="
  # --follow streams the build log, so a failure is visible immediately rather
  # than after a silent wait. --wait makes a failed build fail this script,
  # which is what stops a deploy from rolling out a stale image.
  if [ "$FROM_LOCAL" -eq 1 ]; then
    # .dockerignore keeps this upload small, without it the whole .git
    # directory and the local venv would go over the network every build.
    oc start-build "${svc}" --from-dir=. --follow --wait
  else
    oc start-build "${svc}" --follow --wait
  fi
  echo
done

echo "All builds complete. Images are in the internal registry as ImageStreams:"
oc get imagestream -o custom-columns=NAME:.metadata.name,TAGS:.status.tags[*].tag 2>/dev/null || true

if [ "$FROM_LOCAL" -eq 0 ]; then
  echo
  echo "Each image is traceable to its commit:"
  echo "  oc describe bc/api-gateway | grep -i 'commit\\|ref'"
fi

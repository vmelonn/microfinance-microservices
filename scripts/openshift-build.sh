#!/usr/bin/env bash
#
# Build all nine images inside the OpenShift cluster.
#
#     bash scripts/openshift-build.sh              # build everything
#     bash scripts/openshift-build.sh api-gateway  # rebuild just one
#
# Requires: oc, and an active login (`oc whoami` should succeed).
# Does NOT require a local container engine -- the build happens in the
# cluster, from an upload of the working tree.
#
# Run from the repository root; the whole tree is the build context, because
# every image installs libs/mfcommon as a real package rather than reaching
# it through a sys.path hack.

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

# Build only what was named, or everything.
TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=("${SERVICES[@]}")
fi

# .dockerignore keeps the upload small -- without it the whole .git directory
# and the local venv would be shipped to the cluster on every build.
for svc in "${TARGETS[@]}"; do
  echo "=== ${svc} ==="
  # --follow streams the build log, so a failure is visible immediately
  # rather than after a silent wait. --wait makes a failed build fail this
  # script, which is what stops a deploy from rolling out a stale image.
  oc start-build "${svc}" --from-dir=. --follow --wait
  echo
done

echo "All builds complete. Images are in the internal registry as ImageStreams:"
oc get imagestream -o custom-columns=NAME:.metadata.name,TAGS:.status.tags[*].tag 2>/dev/null || true

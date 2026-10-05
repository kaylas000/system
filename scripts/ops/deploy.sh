#!/usr/bin/env bash
# Build, push and deploy the kernel (specs/06_ops/scripts/DEPLOY.sh — no content in the spec; written by the agent).
#
#   REGISTRY=ghcr.io/acme NAMESPACE=autogen scripts/ops/deploy.sh [tag]
#
# Needs: docker (buildx), helm, kubectl context pointing at the target cluster,
# Secret "autogen-secrets" in $NAMESPACE (see deploy/README.md).
set -euo pipefail
cd "$(dirname "$0")/../.."

REGISTRY="${REGISTRY:?set REGISTRY, e.g. ghcr.io/<owner>}"
NAMESPACE="${NAMESPACE:-autogen}"
RELEASE="${RELEASE:-kernel}"
TAG="${1:-$(git rev-parse --short=12 HEAD)}"
IMAGE="$REGISTRY/autogen-kernel"
VALUES="${VALUES:-}"   # optional extra values file

if [ -n "$(git status --porcelain)" ] && [ "${ALLOW_DIRTY:-0}" != 1 ]; then
  echo "working tree is dirty (ALLOW_DIRTY=1 to override)" >&2
  exit 1
fi

echo "==> build $IMAGE:$TAG"
docker buildx build -f deploy/docker/Dockerfile.kernel -t "$IMAGE:$TAG" --push .

echo "==> migrate (one-off pod)"
kubectl -n "$NAMESPACE" run "kernel-migrate-$TAG" --rm -i --restart=Never --image="$IMAGE:$TAG" \
  --overrides='{"spec":{"containers":[{"name":"m","image":"'"$IMAGE:$TAG"'","command":["python","-m","kernel.main","migrate"],"envFrom":[{"secretRef":{"name":"autogen-secrets"}}]}]}}'

echo "==> helm upgrade $RELEASE"
helm upgrade --install "$RELEASE" deploy/helm/autogen-kernel -n "$NAMESPACE" --create-namespace \
  --set image.repository="$IMAGE" --set image.tag="$TAG" \
  ${VALUES:+-f "$VALUES"} --wait --timeout 10m

kubectl -n "$NAMESPACE" rollout status "deploy/$RELEASE-autogen-kernel"
echo "==> deployed $IMAGE:$TAG"

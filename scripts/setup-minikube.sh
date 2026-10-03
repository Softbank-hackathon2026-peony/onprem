#!/usr/bin/env bash
set -euo pipefail

# 로컬 Minikube를 시작하고 SQS Connector를 배포한다.
# 필요한 값은 환경변수로 전달한다. 비밀값을 파일이나 Git에 저장하지 않는다.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINIKUBE_DRIVER="${MINIKUBE_DRIVER:-docker}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-pawploy}"
NAMESPACE="${ONPREM_NAMESPACE:-pawploy}"
CONNECTOR_IMAGE="${ONPREM_CONNECTOR_IMAGE:-pawploy-connector:dev}"

need_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "필수 명령을 찾을 수 없습니다: $1" >&2
    exit 1
  }
}

need_command minikube
need_command kubectl
need_command docker
# ngrok은 호스트의 tunnel_server.py에서 실행한다.

: "${ONPREM_QUEUE_URL:?ONPREM_QUEUE_URL 환경변수가 필요합니다}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID 환경변수가 필요합니다}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY 환경변수가 필요합니다}"
: "${ONPREM_TUNNEL_TOKEN:?ONPREM_TUNNEL_TOKEN 환경변수가 필요합니다}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
KUBECTL_CONTEXT="${MINIKUBE_PROFILE}"

echo "[1/6] Minikube 시작: profile=${MINIKUBE_PROFILE}, driver=${MINIKUBE_DRIVER}"
minikube status --profile "${MINIKUBE_PROFILE}" >/dev/null 2>&1 || \
  minikube start --profile "${MINIKUBE_PROFILE}" --driver="${MINIKUBE_DRIVER}"

echo "[2/6] namespace 준비"
kubectl --context "${KUBECTL_CONTEXT}" create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl --context "${KUBECTL_CONTEXT}" apply -f -

echo "[3/6] Connector 이미지 빌드"
# minikube image build는 호스트 Docker registry에 push하지 않고 Minikube 런타임에 직접 적재한다.
minikube image build --profile "${MINIKUBE_PROFILE}" -t "${CONNECTOR_IMAGE}" "${ROOT_DIR}"

echo "[4/6] AWS/SQS Secret 갱신"
printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_REGION=%s\nAWS_SESSION_TOKEN=%s\n' \
  "${AWS_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY}" "${AWS_REGION}" "${AWS_SESSION_TOKEN:-}" |
  kubectl --context "${KUBECTL_CONTEXT}" -n "${NAMESPACE}" create secret generic connector-aws \
    --from-env-file=/dev/stdin --dry-run=client -o yaml |
  kubectl --context "${KUBECTL_CONTEXT}" apply -f -
kubectl --context "${KUBECTL_CONTEXT}" -n "${NAMESPACE}" create secret generic connector-config \
  --from-literal=ONPREM_QUEUE_URL="${ONPREM_QUEUE_URL}" \
  --from-literal=ONPREM_TUNNEL_URL="${ONPREM_TUNNEL_URL:-http://host.minikube.internal:8787}" \
  --from-literal=ONPREM_TUNNEL_TOKEN="${ONPREM_TUNNEL_TOKEN:?ONPREM_TUNNEL_TOKEN is required}" \
  --dry-run=client -o yaml | kubectl --context "${KUBECTL_CONTEXT}" apply -f -

echo "[5/6] RBAC 및 Connector Deployment 적용"
sed "s/namespace: pawploy/namespace: ${NAMESPACE}/g" "${ROOT_DIR}/k8s/rbac.yaml" | kubectl --context "${KUBECTL_CONTEXT}" apply -f -
sed "s#image: pawploy-connector:dev#image: ${CONNECTOR_IMAGE}#; s/namespace: pawploy/namespace: ${NAMESPACE}/g; s/value: pawploy/value: ${NAMESPACE}/g" "${ROOT_DIR}/k8s/deployment.yaml" | kubectl --context "${KUBECTL_CONTEXT}" apply -f -

echo "[6/6] 상태 확인"
kubectl --context "${KUBECTL_CONTEXT}" -n "${NAMESPACE}" rollout status deployment/pawploy-connector --timeout=180s
kubectl --context "${KUBECTL_CONTEXT}" -n "${NAMESPACE}" get pods -l app=pawploy-connector

echo "Minikube Connector가 실행되었습니다."

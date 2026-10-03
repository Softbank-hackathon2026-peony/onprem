# On-prem Minikube Connector + ngrok

클라우드 배포가 완료된 화면에서 **온프렘에도 배포**를 요청하면 기존 ECR 이미지를 재사용해 로컬 Minikube에 추가 배포한다. CodeBuild와 Terraform 생성은 다시 실행하지 않는다. 클라우드 배포와 온프렘 배포는 서로 다른 ID로 저장하며 각각 조회·종료할 수 있다.

```text
클라우드 배포 완료 → Backend → S3 jobs/onprem/* + SQS FIFO
                                         ↓
                                Minikube Connector
                                         ↓
                              Deployment + Service
                                         ↓
                    호스트 tunnel_server.py (토큰 인증)
                           kubectl port-forward → ngrok
                                         ↓
                  HTTPS URL → Connector → Backend callback → 웹
```

현재 단일 컨테이너 ECR 이미지가 있는 완료된 AWS EC2, Lambda, GCP Cloud Run 배포를 지원한다. Compose 등 다중 컨테이너 구성은 제외한다. 호스트 중계 서버는 한 번에 앱 하나를 공개하며 다른 앱의 요청은 409로 거절한다. 기존 앱을 종료한 후 다른 앱을 공개한다.

## 추후 실행 준비

아래 명령은 실제 Connector 배포 및 외부 공개를 위한 실행 절차다. 코드 검증에는 실행하지 않는다.

호스트에 Python 3, Docker, Minikube, kubectl, ngrok CLI를 준비한다. 같은 셸에서 Connector와 중계 서버가 공유할 토큰을 생성한다. ngrok 계정 토큰은 호스트에만 제공한다. `NGROK_AUTHTOKEN` 환경변수 또는 `ngrok config add-authtoken`으로 등록한 기본 설정을 사용할 수 있으며, 별도 설정 파일은 `NGROK_CONFIG`로 지정한다.

```bash
export MINIKUBE_PROFILE=pawploy
export ONPREM_TUNNEL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export NGROK_AUTHTOKEN='<ngrok 계정 토큰>'
export ONPREM_QUEUE_URL='https://sqs.ap-northeast-2.amazonaws.com/ACCOUNT/pawploy-onprem.fifo'
export AWS_REGION=ap-northeast-2
export AWS_ACCESS_KEY_ID='<Connector IAM 사용자 키>'
export AWS_SECRET_ACCESS_KEY='<Connector IAM 사용자 비밀키>'
# 임시 AWS 자격 증명이면 AWS_SESSION_TOKEN도 설정한다.
./scripts/setup-minikube.sh
python3 tunnel_server.py
```

중계 서버는 요청을 기다리고, 앱 배포가 준비된 뒤에만 port-forward와 ngrok을 시작한다. Connector는 기본적으로 `http://host.minikube.internal:8787`로 중계 서버에 접근한다. 환경에 따라 `ONPREM_TUNNEL_URL`과 호스트 `ONPREM_TUNNEL_BIND`를 지정한다. 중계 서버 기본 bind는 `0.0.0.0`이며 모든 요청은 `X-Tunnel-Token`으로 인증한다.

호스트의 `18080`은 앱 port-forward, `4041`은 ngrok 로컬 API, `8787`은 중계 제어 서버에 사용한다. ngrok 설정은 임시 파일에 생성하며 계정 토큰은 환경변수로 전달한다. 공개 URL은 ngrok Agent의 `/api/endpoints`에서 읽는다. ngrok 또는 port-forward가 중단됐으면 다음 publish 요청에서 다시 생성한다. 호스트 중계 서버가 종료되면 자신이 시작한 두 프로세스를 정리한다.

ngrok URL 발급 후 배포별 callback URL/token으로 Backend에 보고하고 웹에 표시한다. 온프렘 종료 요청은 해당 터널과 Kubernetes 리소스를 정리한다. 클라우드 리소스는 별도 종료 요청으로 관리한다. 호스트 중계 서버는 공개 접속을 유지하는 동안 실행돼 있어야 한다. URL 발급 이후의 지속적인 터널 상태 감시는 아직 구현하지 않았다.

## Connector 권한

`infra/onprem`은 전용 FIFO 큐와 DLQ, Connector IAM 사용자, Main Server 송신 정책을 정의한다. Connector에는 해당 큐 consume, S3 `jobs/onprem/*` 읽기, ECR 토큰 및 앱 저장소 pull 권한이 필요하다. Kubernetes RBAC는 설정한 namespace의 Deployment, Deployment 상태, Service, ECR Secret 접근을 제공한다.

AWS 키·ngrok 토큰·중계 토큰을 Git이나 Docker 이미지에 넣지 않는다. setup 스크립트는 AWS 키와 중계 설정을 Kubernetes Secret으로 주입한다. 로컬 실행은 `requirements.txt`를 설치하고 `ONPREM_QUEUE_URL`과 AWS 자격 증명, kubeconfig를 제공해 `python connector.py`로 시작한다.

## 로컬 검증

```bash
pip install -r requirements-dev.txt
python -m pytest test_tunnel_server.py -q
bash -n scripts/setup-minikube.sh
```

ngrok API 참고: https://ngrok.com/docs/gateway/agent/api

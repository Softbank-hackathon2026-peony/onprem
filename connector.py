"""SQS 작업을 받아 같은 Minikube 클러스터에 애플리케이션을 배포하는 Connector."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import time
from typing import Any

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from kubernetes import client, config
from kubernetes.client.rest import ApiException


LOG = logging.getLogger("onprem-connector")
VISIBILITY_TIMEOUT = int(os.getenv("ONPREM_VISIBILITY_TIMEOUT", "900"))
WAIT_SECONDS = int(os.getenv("ONPREM_WAIT_SECONDS", "20"))
HEALTH_TIMEOUT = int(os.getenv("ONPREM_HEALTH_TIMEOUT", "300"))
NAME_RE = re.compile(r"[^a-z0-9-]+")


def dns_name(value: str, prefix: str) -> str:
    value = NAME_RE.sub("-", value.lower()).strip("-")
    value = f"{prefix}-{value}".strip("-")
    return value[:63].rstrip("-") or f"{prefix}-app"


class Connector:
    def __init__(self) -> None:
        self.queue_url = os.environ["ONPREM_QUEUE_URL"]
        self.region = os.getenv("AWS_REGION", "ap-northeast-2")
        self.default_namespace = os.getenv("ONPREM_NAMESPACE", "pawploy")
        self.callback_url = os.getenv("ONPREM_STATUS_CALLBACK_URL")
        self.stopping = False
        self.sqs = boto3.client("sqs", region_name=self.region)
        try:
            config.load_incluster_config()
            LOG.info("Kubernetes: in-cluster config")
        except config.ConfigException:
            config.load_kube_config()
            LOG.info("Kubernetes: local kubeconfig")
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()

    def ensure_ecr_pull_secret(self, image_uri: str, namespace: str) -> str | None:
        match = re.match(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/", image_uri)
        if not match:
            return None
        account_id, region = match.groups()
        auth_data = boto3.client("ecr", region_name=region).get_authorization_token(registryIds=[account_id])["authorizationData"][0]
        username, password = base64.b64decode(auth_data["authorizationToken"]).decode().split(":", 1)
        registry = auth_data["proxyEndpoint"].removeprefix("https://")
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        docker_config = json.dumps({"auths": {registry: {"username": username, "password": password, "auth": auth}}})
        secret_name = "pawploy-ecr-pull"
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=namespace),
            type="kubernetes.io/dockerconfigjson",
            data={".dockerconfigjson": base64.b64encode(docker_config.encode()).decode()},
        )
        try:
            self.core.read_namespaced_secret(secret_name, namespace)
            self.core.patch_namespaced_secret(secret_name, namespace, secret)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.core.create_namespaced_secret(namespace, secret)
        return secret_name

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        LOG.info("started queue=%s namespace=%s", self.queue_url, self.default_namespace)
        while not self.stopping:
            try:
                response = self.sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=WAIT_SECONDS,
                    VisibilityTimeout=VISIBILITY_TIMEOUT,
                )
                for message in response.get("Messages", []):
                    self.handle(message)
            except (BotoCoreError, ClientError) as exc:
                LOG.exception("SQS receive failed: %s", exc)
                time.sleep(5)
        LOG.info("stopped")

    def stop(self, _signum: int, _frame: Any) -> None:
        self.stopping = True

    def handle(self, message: dict[str, Any]) -> None:
        receipt = message["ReceiptHandle"]
        context: dict[str, Any] = {}
        try:
            body = json.loads(message["Body"])
            action = body.get("action")
            if action == "deploy":
                job = self.read_job(body["job_uri"])
                context = job
                result = self.deploy(job)
            elif action == "destroy":
                context = body
                result = self.destroy(body)
            else:
                raise ValueError("unsupported action")
            self.report(result, context)
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            LOG.error("invalid message; deleting: %s", exc)
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        except Exception:
            # 일시적인 오류는 visibility timeout 뒤 재처리되도록 메시지를 남긴다.
            LOG.exception("job failed; message will be retried")
            if context.get("deployment_id"):
                try:
                    self.report(self.result(context, "unhealthy", dns_name(str(context["deployment_id"]), "app"),
                        context.get("namespace") or self.default_namespace, reason="Connector operation failed; retry pending"), context)
                except Exception:
                    LOG.exception("Could not report failure")

    def read_job(self, uri: str) -> dict[str, Any]:
        if not uri.startswith("s3://"):
            raise ValueError("job_uri must be an s3:// URI")
        bucket, key = uri[5:].split("/", 1)
        raw = boto3.client("s3", region_name=self.region).get_object(Bucket=bucket, Key=key)["Body"].read()
        job = json.loads(raw.decode("utf-8-sig"))
        job["deployment_id"] = job.get("deployment_id") or job.get("deploy_id")
        if not job["deployment_id"] or not job.get("image_uri"):
            raise ValueError("deployment_id and image_uri are required")
        return job

    def deploy(self, job: dict[str, Any]) -> dict[str, Any]:
        deployment_id = str(job["deployment_id"])
        namespace = str(job.get("namespace") or self.default_namespace)
        name = dns_name(deployment_id, "app")
        port = int(job.get("container_port", 8080))
        replicas = max(1, min(int(job.get("replicas", 1)), 10))
        labels = {"app.kubernetes.io/managed-by": "pawploy-onprem", "pawploy/deployment": name}

        self.report(self.result(job, "applying", name, namespace), job)
        pull_secret = job.get("image_pull_secret") or self.ensure_ecr_pull_secret(job["image_uri"], namespace)
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": {
                "metadata": {"labels": labels}, "spec": {
                    "containers": [{"name": "app", "image": job["image_uri"],
                                    "ports": [{"containerPort": port}],
                                    "env": [{"name": str(k), "value": str(v)} for k, v in (job.get("env") or {}).items()],
                                    "readinessProbe": {"httpGet": {"path": job.get("health_path", "/"), "port": port}, "initialDelaySeconds": 3, "periodSeconds": 5}}]
            }}}
        }
        if pull_secret:
            deployment["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": pull_secret}]
        service = {"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": name, "namespace": namespace, "labels": labels},
                   "spec": {"selector": labels, "ports": [{"port": port, "targetPort": port}],
                             "type": job.get("service_type", "NodePort")}}
        self.apply(self.apps, deployment, client.AppsV1Api, "deployment")
        self.apply(self.core, service, client.CoreV1Api, "service")

        deadline = time.time() + HEALTH_TIMEOUT
        while time.time() < deadline:
            current = self.apps.read_namespaced_deployment_status(name, namespace)
            available = current.status.available_replicas or 0
            if available >= replicas:
                result = self.result(job, "running", name, namespace)
                publisher = os.getenv("ONPREM_TUNNEL_URL")
                if publisher:
                    response = requests.post(f"{publisher.rstrip('/')}/publish",
                        json={"service_name": name, "namespace": namespace, "port": port},
                        headers={"X-Tunnel-Token": os.environ["ONPREM_TUNNEL_TOKEN"]}, timeout=240)
                    response.raise_for_status()
                    result["url"] = response.json()["url"]
                else:
                    result["url"] = os.getenv("ONPREM_BASE_URL", "")
                return result
            time.sleep(5)
        return self.result(job, "failed", name, namespace, reason="deployment readiness timeout")

    def destroy(self, body: dict[str, Any]) -> dict[str, Any]:
        deployment_id = body.get("deployment_id") or body.get("deploy_id")
        if not deployment_id:
            raise ValueError("deployment_id is required")
        namespace = body.get("namespace") or self.default_namespace
        name = dns_name(str(deployment_id), "app")
        publisher = os.getenv("ONPREM_TUNNEL_URL")
        if publisher:
            response = requests.post(f"{publisher.rstrip('/')}/close",
                json={"service_name": name, "namespace": namespace},
                headers={"X-Tunnel-Token": os.environ["ONPREM_TUNNEL_TOKEN"]}, timeout=30)
            response.raise_for_status()
        for api, method in ((self.apps, "delete_namespaced_deployment"), (self.core, "delete_namespaced_service")):
            try:
                getattr(api, method)(name, namespace, propagation_policy="Foreground")
            except ApiException as exc:
                if exc.status != 404:
                    raise
        return {"deployment_id": deployment_id, "project_id": body.get("project_id"), "status": "destroyed",
                "step": "destroyed", "target": "local_kubernetes", "namespace": namespace}

    @staticmethod
    def apply(api: Any, obj: dict[str, Any], _api_type: Any, kind: str) -> None:
        name, namespace = obj["metadata"]["name"], obj["metadata"]["namespace"]
        try:
            getattr(api, f"create_namespaced_{kind}")(namespace, obj)
        except ApiException as exc:
            if exc.status != 409:
                raise
            getattr(api, f"patch_namespaced_{kind}")(name, namespace, obj, field_manager="pawploy-connector")

    @staticmethod
    def result(job: dict[str, Any], status: str, name: str, namespace: str, reason: str | None = None) -> dict[str, Any]:
        step = "health" if status in {"health", "running", "failed"} else "apply" if status in {"applying", "destroying"} else status
        reported_status = "running" if status in {"applying", "health"} else status
        result = {"deployment_id": job["deployment_id"], "project_id": job.get("project_id"), "status": reported_status,
                  "step": step, "target": "local_kubernetes",
                  "namespace": namespace, "service_name": name}
        if reason:
            result["reason"] = reason
        return result

    def report(self, result: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        context = context or {}
        callback_url = context.get("callback_url") or self.callback_url
        callback_token = context.get("callback_token")
        if not callback_url:
            LOG.info("status=%s step=%s deployment=%s", result.get("status"), result.get("step"), result.get("deployment_id"))
            return
        try:
            headers = {"X-Onprem-Callback-Token": callback_token} if callback_token else {}
            response = requests.post(callback_url, json=result, headers=headers, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            LOG.exception("status callback failed")
            raise RuntimeError("Main Server status callback failed") from exc


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Connector().run()

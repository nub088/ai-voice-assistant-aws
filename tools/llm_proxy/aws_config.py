"""Load the AWS deployment's application settings from Secrets Manager."""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3


def load_aws_secrets() -> None:
    arn = os.environ.get("AWS_APP_SECRET_ARN", "")
    if not arn:
        return
    response = boto3.client(
        "secretsmanager", region_name=os.environ.get("AWS_REGION")
    ).get_secret_value(SecretId=arn)
    settings = json.loads(response["SecretString"])
    if not isinstance(settings, dict):
        raise ValueError("AWS app secret must be a JSON object")
    client_config = settings.pop("CLIENT_CONFIG", None)
    google_config = settings.pop("GOOGLE_SERVICE_ACCOUNT", None)
    for key, value in settings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("AWS app secret must contain string keys and values")
        if key.startswith("AWS_") or key in {"ENVIRONMENT", "AWS_ALB_PROXY", "ALLOW_UNSIGNED_TWILIO"}:
            raise ValueError(f"AWS app secret cannot override deployment setting {key}")
        os.environ[key] = value
    google_json = json.dumps(google_config) if google_config else os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if google_json:
        path = "/tmp/voice-google-credentials.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(google_json)
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = path
    if client_config is not None:
        config = client_config
        if not isinstance(config, dict) or not config.get("client_id"):
            raise ValueError("CLIENT_CONFIG_JSON must be a client config object")
        config_dir = Path("config/clients")
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "aws.json").write_text(json.dumps(config), encoding="utf-8")

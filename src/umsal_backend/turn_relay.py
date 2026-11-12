"""IAM-only TURN REST credential issuer. No public URL, SDP or audio."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time


def handler(event, context):
    import boto3
    from botocore.config import Config

    call_id = event.get("call_id", "")
    side = event.get("side")
    if not re.fullmatch(r"call_[a-f0-9]{32}", call_id) or side not in {"caller", "callee"}:
        raise ValueError("Invalid call identity")
    # Only the product API role can invoke this function. Never return or log the secret.
    client = boto3.client(
        "secretsmanager",
        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    secret = client.get_secret_value(SecretId=os.environ["TURN_SECRET_ARN"])["SecretString"]
    expires = int(time.time()) + 3600
    username = f"{expires}:{call_id}:{side}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    address = os.environ["TURN_ADDRESS"]
    return {
        "ice_servers": [
            {
                "urls": [
                    f"turn:{address}:3478?transport=udp",
                    f"turn:{address}:3478?transport=tcp",
                ],
                "username": username,
                "credential": credential,
            }
        ],
        "expires_at": expires,
    }

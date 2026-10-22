"""
Amazon Web Services (Free Tier) — the compliance side of PlateletIQ.

The operational path (alerts to people) runs on QStash, email, Telegram and
Pusher. AWS carries the *record*: an independent, append-only trail of every
custody event that the application itself cannot quietly edit afterwards.

    SNS  ``plateletiq-custody-events``  every transfer timeline event is
         published here. Subscribers: an SQS queue (below) — and anything
         else a regulator might want later, without touching this code.
    SQS  ``plateletiq-custody-ledger`` (+ dead-letter queue) buffers events
         for the Lambda consumer; poison messages land in the DLQ, which a
         CloudWatch alarm watches.
    Lambda + DynamoDB  writes each event into a hash-chained ledger table.
    EventBridge Scheduler + Lambda  e-mails a daily compliance report via
         the ``plateletiq-alerts`` SNS topic.
    S3   chain-of-custody PDF certificates, private, encrypted, served to
         the two facilities through short-lived presigned links.
    SES  one more email provider in the failover chain.
    IAM  this API runs as a user that may only publish to the two topics,
         write certificates, and send mail — nothing else.

All of it is defined in ``aws/plateletiq-aws.yaml`` (CloudFormation). This
module only needs the outputs of that stack in the environment.
"""

import json
from typing import List, Optional

from cloud.config import env

_clients: dict = {}

CERTIFICATE_LINK_SECONDS = 7 * 24 * 3600  # SigV4 presigned URLs max out at 7 days


class AWSError(Exception):
    pass


def region() -> str:
    return env("AWS_REGION") or env("AWS_DEFAULT_REGION") or "ap-south-1"


def has_credentials() -> bool:
    return bool(env("AWS_ACCESS_KEY_ID") and env("AWS_SECRET_ACCESS_KEY"))


def _client(service: str):
    if service not in _clients:
        import boto3
        from botocore.config import Config

        _clients[service] = boto3.client(
            service,
            region_name=region(),
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"}),
        )
    return _clients[service]


def sns_enabled() -> bool:
    return has_credentials() and bool(env("AWS_SNS_CUSTODY_TOPIC_ARN"))


def s3_enabled() -> bool:
    return has_credentials() and bool(env("AWS_S3_BUCKET"))


def ses_enabled() -> bool:
    return has_credentials() and bool(env("AWS_SES_SENDER"))


# ── SNS ──────────────────────────────────────────────────────────────────────

def publish_custody_event(event: dict) -> str:
    """Publish one custody event to the ledger topic. Returns the SNS message id."""
    topic = env("AWS_SNS_CUSTODY_TOPIC_ARN")
    if not (topic and has_credentials()):
        raise AWSError("AWS_SNS_CUSTODY_TOPIC_ARN or AWS credentials are not set.")
    res = _client("sns").publish(
        TopicArn=topic,
        Message=json.dumps(event, default=str),
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": str(event.get("status") or "UNKNOWN")},
            "transfer_id": {"DataType": "String", "StringValue": str(event.get("transfer_id") or "-")},
        },
    )
    return res.get("MessageId", "")


def publish_alert(subject: str, message: str) -> Optional[str]:
    """Send an operational alert to the compliance topic (e-mail subscribers)."""
    topic = env("AWS_SNS_ALERTS_TOPIC_ARN")
    if not (topic and has_credentials()):
        return None
    res = _client("sns").publish(TopicArn=topic, Subject=subject[:99], Message=message)
    return res.get("MessageId", "")


# ── S3 ───────────────────────────────────────────────────────────────────────

def put_object(key: str, data: bytes, content_type: str) -> dict:
    bucket = env("AWS_S3_BUCKET")
    if not (bucket and has_credentials()):
        raise AWSError("AWS_S3_BUCKET or AWS credentials are not set.")
    _client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return {"bucket": bucket, "key": key, "url": presign(key)}


def presign(key: str, seconds: int = CERTIFICATE_LINK_SECONDS) -> str:
    return _client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": env("AWS_S3_BUCKET"), "Key": key},
        ExpiresIn=seconds,
    )


# ── SES ──────────────────────────────────────────────────────────────────────

def ses_send(to: List[str], subject: str, html: str, text: str, sender: str) -> str:
    res = _client("sesv2").send_email(
        FromEmailAddress=sender,
        Destination={"ToAddresses": to},
        Content={
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html, "Charset": "UTF-8"},
                    "Text": {"Data": text or subject, "Charset": "UTF-8"},
                },
            }
        },
    )
    return res.get("MessageId", "")


# ── Diagnostics ──────────────────────────────────────────────────────────────

def whoami() -> dict:
    """STS identity — proves the keys work without touching any resource."""
    identity = _client("sts").get_caller_identity()
    return {"account": identity.get("Account"), "arn": identity.get("Arn")}


def status() -> dict:
    return {
        "region": region(),
        "credentials": has_credentials(),
        "sns_custody": bool(env("AWS_SNS_CUSTODY_TOPIC_ARN")),
        "sns_alerts": bool(env("AWS_SNS_ALERTS_TOPIC_ARN")),
        "s3_bucket": env("AWS_S3_BUCKET") or None,
        "ses_sender": bool(env("AWS_SES_SENDER")),
    }

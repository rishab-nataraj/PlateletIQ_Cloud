"""
The AWS side, end to end, against moto's in-memory AWS:

    backend cloud.aws.publish_custody_event
        -> SNS custody topic -> SQS ledger queue (raw delivery)
        -> custody_ledger.handler -> DynamoDB hash chain
        -> compliance_report.handler -> SNS alerts topic

Run from the repository root:  pytest aws/tests
"""

import datetime
import importlib.util
import json
import os
import pathlib
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

REGION = "ap-south-1"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "aws" / "lambdas" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        sns, sqs, ddb = (boto3.client(s, region_name=REGION) for s in ("sns", "sqs", "dynamodb"))
        custody = sns.create_topic(Name="plateletiq-custody-events")["TopicArn"]
        alerts = sns.create_topic(Name="plateletiq-alerts")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="plateletiq-custody-ledger")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=custody, Protocol="sqs", Endpoint=queue_arn, Attributes={"RawMessageDelivery": "true"})
        # Capture what the report publishes.
        report_queue = sqs.create_queue(QueueName="alerts-capture")["QueueUrl"]
        report_arn = sqs.get_queue_attributes(QueueUrl=report_queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=alerts, Protocol="sqs", Endpoint=report_arn)

        ddb.create_table(
            TableName="plateletiq-custody-ledger",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "day", "ts")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
            GlobalSecondaryIndexes=[{
                "IndexName": "ByDay",
                "KeySchema": [{"AttributeName": "day", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {"ReadCapacityUnits": 2, "WriteCapacityUnits": 2},
            }],
        )
        monkeypatch.setenv("TABLE_NAME", "plateletiq-custody-ledger")
        monkeypatch.setenv("ALERTS_TOPIC_ARN", alerts)
        monkeypatch.setenv("AWS_SNS_CUSTODY_TOPIC_ARN", custody)
        yield {"sqs": sqs, "ddb": ddb, "queue": queue_url, "report_queue": report_queue}


def _drain(sqs, queue_url):
    records = []
    while True:
        batch = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        if not batch:
            return records
        for m in batch:
            records.append({"messageId": m["MessageId"], "body": m["Body"]})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])


def _event(transfer_id, n, status):
    when = datetime.datetime.utcnow() - datetime.timedelta(minutes=30 - n)
    return {
        "event_id": f"{transfer_id}-{n}",
        "transfer_id": transfer_id,
        "status": status,
        "title": status.title(),
        "occurred_at": when.isoformat() + "Z",
    }


def test_custody_events_flow_into_a_verified_hash_chain(stack):
    from cloud import aws

    aws._clients.clear()
    ledger = _load("custody_ledger")
    report = _load("compliance_report")

    statuses = ["REQUESTED", "UNITS_RESERVED", "IN_TRANSIT", "TRANSFER_COMPLETED"]
    for n, status in enumerate(statuses):
        aws.publish_custody_event(_event("TRF-AAAA1111", n, status))
    aws.publish_custody_event(_event("TRF-BBBB2222", 0, "REQUESTED"))

    records = _drain(stack["sqs"], stack["queue"])
    assert len(records) == 5
    result = ledger.handler({"Records": records}, None)
    assert result == {"batchItemFailures": []}

    # SQS is at-least-once: replaying the same messages must change nothing.
    assert ledger.handler({"Records": records}, None) == {"batchItemFailures": []}

    items = stack["ddb"].query(
        TableName="plateletiq-custody-ledger",
        KeyConditionExpression="pk = :p AND begins_with(sk, :e)",
        ExpressionAttributeValues={":p": {"S": "TRANSFER#TRF-AAAA1111"}, ":e": {"S": "EVENT#"}},
    )["Items"]
    assert [i["status"]["S"] for i in items] == statuses
    assert items[0]["prev_hash"]["S"] == "GENESIS"
    assert all(items[k]["prev_hash"]["S"] == items[k - 1]["hash"]["S"] for k in range(1, len(items)))

    summary = report.handler({}, None)
    assert summary == {"events": 5, "transfers": 2, "broken": []}
    message = json.loads(_drain(stack["sqs"], stack["report_queue"])[0]["body"])["Message"]
    assert "all 2 chain(s) verified" in message
    assert "Transfers settled:     1" in message


def test_tampering_is_detected_by_the_report(stack):
    from cloud import aws

    aws._clients.clear()
    ledger = _load("custody_ledger")
    report = _load("compliance_report")

    for n, status in enumerate(["REQUESTED", "TRANSFER_COMPLETED"]):
        aws.publish_custody_event(_event("TRF-TAMPER01", n, status))
    ledger.handler({"Records": _drain(stack["sqs"], stack["queue"])}, None)

    # Someone edits a stored event after the fact.
    stack["ddb"].update_item(
        TableName="plateletiq-custody-ledger",
        Key={"pk": {"S": "TRANSFER#TRF-TAMPER01"}, "sk": {"S": "EVENT#000001"}},
        UpdateExpression="SET payload = :p",
        ExpressionAttributeValues={":p": {"S": '{"status":"REQUESTED","units":999}'}},
    )
    assert report.handler({}, None)["broken"] == ["TRF-TAMPER01"]


def test_a_poison_message_is_reported_for_redrive(stack):
    ledger = _load("custody_ledger")
    result = ledger.handler({"Records": [{"messageId": "m-1", "body": "not json"}]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}


def test_inline_lambda_code_fits_cloudformation_limits():
    for path in (ROOT / "aws" / "lambdas").glob("*.py"):
        assert len(path.read_text().rstrip("\n")) <= 4096, path.name

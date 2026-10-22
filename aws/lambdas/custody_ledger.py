import hashlib,json,os,boto3
from botocore.exceptions import ClientError
# PlateletIQ custody ledger. SQS -> this Lambda -> DynamoDB.
# Each transfer is a hash chain: hash_n = sha256(hash_n-1 + payload_n).
# One transaction writes the event, advances the chain head (optimistic
# lock on seq) and claims the event id, so SQS redelivery is a no-op.
D=boto3.client("dynamodb")
T=os.environ["TABLE_NAME"]
def S(v):return{"S":str(v)}
def append(e):
  tid=str(e.get("transfer_id") or "UNKNOWN")
  body=json.dumps(e,sort_keys=True,separators=(",",":"),default=str)
  eid=str(e.get("event_id") or hashlib.sha256(body.encode()).hexdigest())
  ts=str(e.get("occurred_at") or "")
  pk="TRANSFER#"+tid
  for _ in range(6):
    h=D.get_item(TableName=T,Key={"pk":S(pk),"sk":S("HEAD")},ConsistentRead=True).get("Item")
    seq=int(h["seq"]["N"]) if h else 0
    prev=h["hash"]["S"] if h else "GENESIS"
    dig=hashlib.sha256((prev+body).encode()).hexdigest()
    item={"pk":S(pk),"sk":S("EVENT#%06d"%(seq+1)),"day":S(ts[:10] or "unknown"),"ts":S(ts),
      "event_id":S(eid),"status":S(e.get("status")),"transfer_id":S(tid),"payload":S(body),
      "prev_hash":S(prev),"hash":S(dig)}
    head={"TableName":T,"Item":{"pk":S(pk),"sk":S("HEAD"),"seq":{"N":str(seq+1)},"hash":S(dig)}}
    if h:head.update(ConditionExpression="seq = :s",ExpressionAttributeValues={":s":{"N":str(seq)}})
    else:head["ConditionExpression"]="attribute_not_exists(pk)"
    try:
      D.transact_write_items(TransactItems=[
        {"Put":{"TableName":T,"Item":{"pk":S("EVENTID#"+eid),"sk":S("ID")},"ConditionExpression":"attribute_not_exists(pk)"}},
        {"Put":{"TableName":T,"Item":item,"ConditionExpression":"attribute_not_exists(sk)"}},
        {"Put":head}])
      print(json.dumps({"ledger":"appended","transfer":tid,"seq":seq+1,"status":e.get("status")}))
      return
    except ClientError as x:
      r=x.response.get("CancellationReasons") or []
      if r and r[0].get("Code")=="ConditionalCheckFailed":
        print(json.dumps({"ledger":"duplicate","event":eid}));return
      if x.response["Error"]["Code"]!="TransactionCanceledException":raise
  raise RuntimeError("ledger head contention on "+tid)
def handler(event,context):
  fails=[]
  for r in event.get("Records",[]):
    try:
      b=json.loads(r["body"])
      if "TopicArn" in b and "Message" in b:b=json.loads(b["Message"])
      append(b)
    except Exception as x:
      print(json.dumps({"ledger":"error","id":r.get("messageId"),"error":str(x)}))
      fails.append({"itemIdentifier":r["messageId"]})
  return{"batchItemFailures":fails}

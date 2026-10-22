import datetime,hashlib,os,boto3
from collections import Counter
# PlateletIQ daily compliance report. EventBridge Scheduler -> this Lambda.
# Reads the last 24 h of custody events from DynamoDB (ByDay index),
# re-verifies each touched transfer's hash chain end to end, and e-mails
# the summary through the alerts SNS topic.
D=boto3.client("dynamodb")
N=boto3.client("sns")
T=os.environ["TABLE_NAME"]
A=os.environ["ALERTS_TOPIC_ARN"]
def q(**k):
  out=[]
  for page in D.get_paginator("query").paginate(TableName=T,**k):out+=page["Items"]
  return out
def intact(pk):
  prev="GENESIS"
  for i in q(KeyConditionExpression="pk = :p AND begins_with(sk, :e)",
             ExpressionAttributeValues={":p":{"S":pk},":e":{"S":"EVENT#"}}):
    if i["prev_hash"]["S"]!=prev:return False
    if hashlib.sha256((prev+i["payload"]["S"]).encode()).hexdigest()!=i["hash"]["S"]:return False
    prev=i["hash"]["S"]
  return True
def handler(event,context):
  now=datetime.datetime.utcnow()
  since=(now-datetime.timedelta(hours=24)).isoformat()
  items=[]
  for d in sorted({now.date().isoformat(),(now-datetime.timedelta(days=1)).date().isoformat()}):
    items+=q(IndexName="ByDay",KeyConditionExpression="#d = :d AND #t >= :s",
             ExpressionAttributeNames={"#d":"day","#t":"ts"},
             ExpressionAttributeValues={":d":{"S":d},":s":{"S":since}})
  st=Counter(i["status"]["S"] for i in items)
  pks=sorted({i["pk"]["S"] for i in items})
  bad=[p[9:] for p in pks if not intact(p)]
  lines=["PlateletIQ custody ledger - last 24 hours to %s UTC"%now.strftime("%Y-%m-%d %H:%M"),"",
    "Events recorded:       %d"%len(items),
    "Transfers touched:     %d"%len(pks),
    "Requests opened:       %d"%st.get("REQUESTED",0),
    "Transfers settled:     %d"%st.get("TRANSFER_COMPLETED",0),
    "Declined/cancelled/failed: %d"%(st.get("DECLINED",0)+st.get("CANCELLED",0)+st.get("FAILED",0)),"",
    "Hash-chain integrity:  "+("all %d chain(s) verified"%len(pks) if not bad else "BROKEN for "+", ".join(bad)),
    "","Events by status:"]
  lines+=["  %-24s %d"%(k,v) for k,v in st.most_common()]
  N.publish(TopicArn=A,Subject=("PlateletIQ compliance report "+now.date().isoformat())[:99],Message="\n".join(lines))
  return{"events":len(items),"transfers":len(pks),"broken":bad}

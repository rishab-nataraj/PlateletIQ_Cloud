# PlateletIQ on AWS (Free Tier)

One CloudFormation template, `plateletiq-aws.yaml`, creates the compliance side
of PlateletIQ. You deploy it by uploading that single file in the AWS console —
no CLI, no S3 bucket, no SAM.

```
 PlateletIQ API (Render)
   │  every committed transfer event
   ▼
 SNS  plateletiq-custody-events ──► SQS  plateletiq-custody-ledger ──(3 failures)──► SQS DLQ
                                          │                                            │
                                          ▼                                            ▼
                                    Lambda  custody-ledger                  CloudWatch alarm
                                          │                                            │
                                          ▼                                            ▼
                               DynamoDB  plateletiq-custody-ledger         SNS plateletiq-alerts ──► email
                               (hash chain per transfer)                         ▲
                                          ▲                                      │
 EventBridge Scheduler 08:00 IST ──► Lambda compliance-report ───────────────────┘
                                     (re-verifies every chain)

 S3   plateletiq-certificates-…  chain-of-custody PDFs (private, encrypted, versioned)
 SES  optional sender identity    4th email provider for the API
 IAM  plateletiq-backend          may only: sns:Publish (2 topics), s3:Put/Get (2 prefixes), ses:SendEmail
 AWS Budgets                      email at 80% of US$1/month — so the free tier stays free
```

## Deploy (about 10 minutes)

1. Sign in to the AWS console and switch the region to **Asia Pacific (Mumbai) ap-south-1**.
2. **CloudFormation → Create stack → With new resources → Upload a template file** →
   choose `aws/plateletiq-aws.yaml`.
3. Stack name `plateletiq`. Parameters:
   - **AlertEmail** — where the daily compliance report, alarms and budget warnings go.
   - **SesSenderEmail** — optional; an address to verify for Amazon SES.
   - **MonthlyBudgetUsd** — leave at `1`.
4. Tick *"I acknowledge that AWS CloudFormation might create IAM resources with custom names"* → **Submit**.
5. When the stack reaches `CREATE_COMPLETE`:
   - Open the **"AWS Notification - Subscription Confirmation"** email and click *Confirm subscription*.
     If you set SesSenderEmail, also click the SES verification email.
   - **IAM → Users → plateletiq-backend → Security credentials → Create access key →
     "Application running outside AWS"**. Copy both values once.
6. Copy the stack's **Outputs → ApiEnvironment** line plus the two keys into the API's
   environment (Render or Doppler):

   ```
   AWS_REGION=ap-south-1
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   AWS_SNS_CUSTODY_TOPIC_ARN=arn:aws:sns:ap-south-1:...:plateletiq-custody-events
   AWS_SNS_ALERTS_TOPIC_ARN=arn:aws:sns:ap-south-1:...:plateletiq-alerts
   AWS_S3_BUCKET=plateletiq-certificates-...
   AWS_SES_SENDER=you@example.com      # only if you verified one
   ```

## Check it works

- Cloud services screen → **Amazon SNS → Test**. Within seconds:
  **DynamoDB → Tables → plateletiq-custody-ledger → Explore items** shows a `TRANSFER#TEST-…` item.
- Run a transfer to completion in the console. Its events appear under `TRANSFER#TRF-…` as
  `EVENT#000001…`, each with `prev_hash` equal to the previous item's `hash`.
- **Lambda → plateletiq-compliance-report → Test** (any event) sends the report email now
  instead of waiting for 08:00.
- **CloudWatch → Log groups → /aws/lambda/plateletiq-custody-ledger** shows one JSON line per event.

## Changing the Lambda code

The Lambda sources live in `aws/lambdas/`. After editing them (or `template.src.yaml`) run
`python aws/build.py` to regenerate `plateletiq-aws.yaml`; CI fails if you forget. Tests:
`pytest aws/tests` runs the whole SNS → SQS → Lambda → DynamoDB → report flow against
moto's in-memory AWS, including replayed messages and a tampered ledger row.

## Removing it

Delete the `plateletiq` stack. The certificates bucket is kept on purpose (it is a
compliance record): empty and delete it from the S3 console if you want it gone.

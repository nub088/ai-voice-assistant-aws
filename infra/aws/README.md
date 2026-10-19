# AWS voice assistant variant

This variant is derived from [the original voice assistant](https://github.com/nub088/ai-voice-assistant).
It keeps the Twilio
phone interface and Pipecat audio pipeline, and moves hosting, model inference,
storage, secrets, and post-call analysis to AWS. The application and its AWS
infrastructure **have been deployed** on AWS and serve real customers.
Controlled load testing has not been completed. See the
[production results](../../README.md#production-results) for actual call,
booking, and revenue figures.

| Existing component | AWS variant |
| --- | --- |
| Fly.io machine | ECS Fargate task behind an Application Load Balancer |
| Fly secrets | Secrets Manager JSON secret, loaded by the task IAM role |
| Anthropic API for live LLM | Pipecat AWS Bedrock adapter and ECS task role |
| SQLite on Fly volume | Redacted transcripts in S3; metadata, usage, and minute counters in DynamoDB |
| In-process-only call work | Live voice agent plus event-driven Bedrock post-call analysis agent |
| Fly logs | CloudWatch Logs |
| Fly build/deploy | ECR image and CloudFormation stack |

Call flow: Twilio webhook → HTTPS ALB → Fargate FastAPI → Twilio Media Streams
WebSocket → Deepgram speech recognition → Bedrock LLM with booking and optional
knowledge-base tools → Cartesia speech synthesis → Twilio. At call end, PII
redaction happens before writing to S3. An S3 event invokes the Bedrock
post-call agent, which writes structured analysis to a separate S3 prefix.
EventBridge also runs a daily retention pass. The app continues to use Google
Sheets and Twilio SMS for its existing booking and notification workflows.

The optional `retrieve_knowledge` function uses an existing Bedrock Knowledge
Base ID supplied as `<CLIENT_PREFIX>_BEDROCK_KNOWLEDGE_BASE_ID` in the secret.
The stack does not create or populate a Knowledge Base. This is a cascaded
STT → LLM → TTS voice path; it does not implement Bedrock Nova Sonic's direct
speech-to-speech API. Twilio handles the phone network and streams audio over
WebSocket. The app does not terminate SIP or RTP, and its current call path
does not use WebRTC or Amazon Connect.

## Before deploying

You need an AWS account with CLI credentials, Docker, `zip`, and a Bedrock
model available to your account in the chosen region. You also need a
Twilio number, Deepgram and Cartesia credentials, a Google service account and
Sheet, a domain name, and an ACM certificate for that domain in the same AWS
region. Create a Secrets Manager secret containing a JSON object based on
`secret.example.json`; use real values and keep that file outside Git. The
`CLIENT_CONFIG` object is written into the container at startup because the
original repository intentionally excludes live client configurations.

The example configuration is for a pest-control demo. Change its number,
business identity, hours, and prompt paths for your tenant. A `live: true`
tenant must have a working Google Sheet; `/healthz` returns 503 otherwise.
The secret must also include `VOICEMAIL_URL_SECRET`, the Twilio auth token,
Deepgram and Cartesia keys, and the tenant-specific Sheet ID. All secret
values other than `CLIENT_CONFIG` and `GOOGLE_SERVICE_ACCOUNT` are strings.

## Deploy

From the repository root, set `AWS_REGION` and run:

```bash
export AWS_REGION=us-east-1
bash infra/aws/deploy.sh \
  my-voice-stack \
  voice.example.com \
  arn:aws:acm:us-east-1:123456789012:certificate/REPLACE \
  arn:aws:secretsmanager:us-east-1:123456789012:secret:REPLACE \
  REPLACE_WITH_BEDROCK_MODEL_ID \
  OPTIONAL_ROUTE53_HOSTED_ZONE_ID
```

The script creates the base stack first, builds and pushes the image to ECR,
uploads the Lambda package, then creates the ECS service and worker. If you
omit the hosted zone ID, create a DNS alias/CNAME pointing your hostname to
the output ALB DNS name. Configure the Twilio voice webhook with the stack's
`VoiceWebhook` output after DNS and `/healthz` work. Test with a nonproduction
Twilio number before moving real calls. The script creates billable AWS
resources; it is intentionally not run by this repository.

The Fargate task is in public subnets to reach Twilio, Deepgram, Cartesia,
Google, and AWS APIs without a NAT Gateway. Its security group accepts only
port 8080 from the ALB. TLS terminates at the ALB. The task role grants only
the required DynamoDB, S3, Secrets Manager, and Bedrock actions. The Bedrock
model invocation policy uses `Resource: '*'` because a supplied model ID may
refer to a foundation model or an inference profile; narrow it once you select
the exact model. Use an AWS-managed key for the app secret, or add `kms:Decrypt`
to the task role for a customer-managed key.

## Limits and cost

This mirrors the original app's single-process design: one ECS task handles
calls and keeps active call and admission state in memory. Deployments use a
single-task replacement, so a deployment or task failure can interrupt active
calls. DynamoDB persists completed minute totals but does not make in-flight
admission globally atomic. Scale-out needs a shared admission store and call
draining strategy before raising `DesiredCount` above one.

The current DynamoDB retention and last-four-digit deletion paths scan the
table. They are suitable for a small demo, not a large contact centre; add
indexes or a separate retention stream as volume grows. S3 objects have a
90-day lifecycle as a backstop, and the scheduled agent removes matching
DynamoDB records daily. Provider usage and transcript data from the AWS
backend are not yet supported by the original SQLite-only `cost_per_call.py`
operator script. The Fly-only Twilio recording retention job also needs a
separate AWS schedule before production use with voicemail recording.

Always-on Fargate, the ALB, CloudWatch, Secrets Manager, DynamoDB, S3, Bedrock,
Twilio, Deepgram, and Cartesia can all incur charges. The deployment script
provisions AWS resources for each new stack.

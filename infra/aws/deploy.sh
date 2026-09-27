#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 || $# -gt 6 ]]; then
  echo "Usage: $0 STACK DOMAIN CERT_ARN APP_SECRET_ARN BEDROCK_MODEL_ID [HOSTED_ZONE_ID]" >&2
  exit 2
fi

STACK_NAME="$1"
DOMAIN_NAME="$2"
CERT_ARN="$3"
APP_SECRET_ARN="$4"
MODEL_ID="$5"
HOSTED_ZONE_ID="${6:-}"
AWS_REGION="${AWS_REGION:?Set AWS_REGION to the deployment region}"
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$ROOT_DIR/infra/aws/template.yaml"

if ! aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" >/dev/null 2>&1; then
  aws cloudformation deploy \
    --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      "CreateService=false" "DomainName=$DOMAIN_NAME" "CertificateArn=$CERT_ARN" \
      "AppSecretArn=$APP_SECRET_ARN" "BedrockModelId=$MODEL_ID" "HostedZoneId=$HOSTED_ZONE_ID"
fi

output() {
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

REPOSITORY_URI="$(output RepositoryUri)"
BUCKET_NAME="$(output TranscriptBucketName)"
REGISTRY="${REPOSITORY_URI%%/*}"
IMAGE_TAG="$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="$REPOSITORY_URI:$IMAGE_TAG"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker build -f "$ROOT_DIR/infra/aws/Dockerfile" -t "$IMAGE_URI" "$ROOT_DIR"
docker push "$IMAGE_URI"

ARTIFACT_DIR="$(mktemp -d)"
cp "$ROOT_DIR/infra/aws/post_call_agent.py" "$ARTIFACT_DIR/post_call_agent.py"
(
  cd "$ARTIFACT_DIR"
  zip -q post-call-agent.zip post_call_agent.py
)
AGENT_CODE_KEY="deployment/post-call-agent-$IMAGE_TAG.zip"
aws s3 cp "$ARTIFACT_DIR/post-call-agent.zip" "s3://$BUCKET_NAME/$AGENT_CODE_KEY" --region "$AWS_REGION"

aws cloudformation deploy \
  --region "$AWS_REGION" --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    "CreateService=true" "ImageUri=$IMAGE_URI" "AgentCodeKey=$AGENT_CODE_KEY" "DomainName=$DOMAIN_NAME" \
    "CertificateArn=$CERT_ARN" "AppSecretArn=$APP_SECRET_ARN" \
    "BedrockModelId=$MODEL_ID" "HostedZoneId=$HOSTED_ZONE_ID"

echo "Webhook: $(output VoiceWebhook)"
echo "Image: $IMAGE_URI"

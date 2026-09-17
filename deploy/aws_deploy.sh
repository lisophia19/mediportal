#!/bin/bash
# Provisions a single EC2 instance running the full mediportal stack
# (Postgres + backend + frontend via docker-compose) behind Caddy for
# automatic free HTTPS via a nip.io hostname -- no domain purchase needed.
#
# Requires: `aws configure` already run with real credentials, and the repo
# pushed to GitHub (this script clones it fresh on the instance).
#
# Usage: ./deploy/aws_deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-us-east-1}"
INSTANCE_TYPE="t3.micro"
REPO_URL="https://github.com/lisophia19/mediportal.git"
NAME_TAG="mediportal-demo"

if [ ! -f .env ]; then
  echo ".env not found -- copy .env.example to .env and fill in real values first." >&2
  exit 1
fi

echo "==> Looking up latest Ubuntu 22.04 AMI for $REGION"
AMI_ID=$(aws ssm get-parameters \
  --region "$REGION" \
  --names /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
  --query 'Parameters[0].Value' --output text)
echo "    $AMI_ID"

echo "==> Finding default VPC"
VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
if [ "$VPC_ID" = "None" ]; then
  echo "No default VPC found in $REGION -- create one or pass an existing VPC/subnet manually." >&2
  exit 1
fi

echo "==> Creating security group (idempotent)"
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters Name=group-name,Values="$NAME_TAG-sg" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ]; then
  SG_ID=$(aws ec2 create-security-group --region "$REGION" \
    --group-name "$NAME_TAG-sg" --description "mediportal demo" --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --ip-permissions \
    'IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]' \
    'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
    'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]' \
    > /dev/null
  echo "    created $SG_ID (22/80/443 open)"
else
  echo "    reusing $SG_ID"
fi

echo "==> Creating key pair (idempotent, saved to deploy/mediportal-key.pem)"
if [ ! -f deploy/mediportal-key.pem ]; then
  aws ec2 create-key-pair --region "$REGION" --key-name "$NAME_TAG-key" \
    --query 'KeyMaterial' --output text > deploy/mediportal-key.pem
  chmod 400 deploy/mediportal-key.pem
  echo "    created deploy/mediportal-key.pem"
else
  echo "    reusing deploy/mediportal-key.pem"
fi

echo "==> Building user-data (embeds .env contents -- never printed, never committed)"
USER_DATA_FILE=$(mktemp)
trap 'rm -f "$USER_DATA_FILE"' EXIT

{
  echo '#!/bin/bash'
  echo 'set -e'
  echo 'apt-get update -y'
  echo 'apt-get install -y git curl debian-keyring debian-archive-keyring apt-transport-https gnupg'
  echo '# Ubuntu 22.04'"'"'s own repos do not carry docker-compose-plugin under'
  echo '# that name -- use Docker'"'"'s official install script, which sets up'
  echo '# their apt repo correctly and includes the compose plugin.'
  echo 'curl -fsSL https://get.docker.com | sh'
  echo 'systemctl enable --now docker'
  echo "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  echo "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list"
  echo 'apt-get update -y'
  echo 'apt-get install -y caddy'
  echo "git clone $REPO_URL /opt/mediportal"
  echo 'cd /opt/mediportal'
  echo 'cat > .env << '"'"'ENVEOF'"'"''
  cat .env
  echo 'ENVEOF'
  echo 'PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)'
  echo 'NIP_HOST=$(echo $PUBLIC_IP | tr "." "-").nip.io'
  echo 'echo "VITE_API_BASE_URL=https://$NIP_HOST/api/v1" >> .env'
  echo 'docker compose up -d --build'
  echo 'echo "==> waiting for backend to be ready before seeding"'
  echo 'for i in $(seq 1 30); do curl -sf http://localhost:5000/api/v1/health > /dev/null && break; sleep 5; done'
  echo '# seed_mediportal.py expects to sit next to a backend/ dir and a'
  echo '# data/ dir holding the xlsx (repo-root layout) -- inside the'
  echo '# container /app IS backend/ itself, so recreate that layout via a'
  echo '# symlink before running it.'
  echo 'docker compose exec -T backend mkdir -p /opt/seed_run/data'
  echo 'docker compose exec -T backend ln -sfn /app /opt/seed_run/backend'
  echo 'docker compose cp seed_mediportal.py "backend:/opt/seed_run/seed_mediportal.py"'
  echo 'docker compose cp "data/Mediportal Information FINAL.xlsx" "backend:/opt/seed_run/data/Mediportal Information FINAL.xlsx"'
  echo 'docker compose exec -T -w /opt/seed_run backend python3 seed_mediportal.py'
  echo 'cat > /etc/caddy/Caddyfile << CADDYEOF'
  echo '$NIP_HOST {'
  echo '    handle /api/* {'
  echo '        reverse_proxy localhost:5000'
  echo '    }'
  echo '    handle /webhooks/* {'
  echo '        reverse_proxy localhost:5000'
  echo '    }'
  echo '    handle {'
  echo '        reverse_proxy localhost:8080'
  echo '    }'
  echo '}'
  echo 'CADDYEOF'
  echo 'systemctl restart caddy'
  echo 'echo "https://$NIP_HOST" > /home/ubuntu/backend_url.txt'
} > "$USER_DATA_FILE"

echo "==> Launching $INSTANCE_TYPE instance"
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --key-name "$NAME_TAG-key" --security-group-ids "$SG_ID" \
  --user-data "file://$USER_DATA_FILE" \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_TAG}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "    $INSTANCE_ID"

echo "==> Waiting for instance to be running"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
NIP_HOST="$(echo "$PUBLIC_IP" | tr '.' '-').nip.io"
BACKEND_URL="https://$NIP_HOST/api/v1"

echo ""
echo "==> Instance launched: $INSTANCE_ID ($PUBLIC_IP)"
echo "    Backend URL (once boot + docker build + cert issuance finish, ~3-5 min):"
echo "    $BACKEND_URL"
echo ""
echo "==> Polling health endpoint (this will take a few minutes)..."
for i in $(seq 1 40); do
  if curl -sf "$BACKEND_URL/health" > /dev/null 2>&1; then
    echo "READY: $BACKEND_URL"
    exit 0
  fi
  sleep 15
done
echo "Not up yet after 10 minutes -- SSH in to check cloud-init progress:"
echo "  ssh -i deploy/mediportal-key.pem ubuntu@$PUBLIC_IP"
echo "  sudo cloud-init status --long"
echo "  sudo docker compose -f /opt/mediportal/docker-compose.yml logs"

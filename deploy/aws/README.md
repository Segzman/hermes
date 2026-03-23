# Hermes AWS Secret Setup

Use one of these two storage patterns for Hermes runtime config on EC2:

- SSM Parameter Store path: `/hermes/prod`
- Secrets Manager secret: `hermes/prod/shared`

Keep these bootstrap values local in `~/hermes/.env` on the EC2:

```dotenv
AWS_REGION=us-east-1
HERMES_AWS_SSM_PATH=/hermes/prod
# or:
HERMES_AWS_SECRET_ID=hermes/prod/shared
```

## IAM Reader Policy For The EC2 Role

Attach [hermes-runtime-reader-policy.json](/Users/sekun/hermes/deploy/aws/hermes-runtime-reader-policy.json) to the instance role.

If you encrypt SSM parameters or Secrets Manager values with a **customer-managed KMS key**, also grant `kms:Decrypt` on that key.

## Push Current `.env` Values To SSM

```bash
cd ~/hermes
./scripts/push_runtime_env_to_ssm.sh /hermes/prod .env
```

This uploads the current Hermes runtime keys as:

- `/hermes/prod/LLM_PROVIDER`
- `/hermes/prod/OPENROUTER_API_KEY`
- `/hermes/prod/OPENROUTER_MODEL`
- `/hermes/prod/BEDROCK_API_KEY`
- `/hermes/prod/BEDROCK_BASE_URL`
- `/hermes/prod/BEDROCK_MODEL`
- `/hermes/prod/BEDROCK_VISION_MODEL`
- `/hermes/prod/VOICE_TRANSCRIBE_MODEL`
- `/hermes/prod/SERPER_API_KEY`
- `/hermes/prod/SLATE_URL`
- `/hermes/prod/TELEGRAM_BOT_TOKEN`
- `/hermes/prod/TELEGRAM_CHAT_ID`
- `/hermes/prod/APPLE_ID`
- `/hermes/prod/APPLE_APP_PASSWORD`
- `/hermes/prod/APPLE_REMINDERS_NAME`
- `/hermes/prod/APPLE_CALENDAR_NAME`
- `/hermes/prod/BROWSER_BACKEND`
- `/hermes/prod/BROWSERBASE_API_KEY`
- `/hermes/prod/BROWSERBASE_PROJECT_ID`
- `/hermes/prod/BROWSERBASE_KEEP_ALIVE`
- `/hermes/prod/BROWSERBASE_CONTEXT_ID`
- `/hermes/prod/CHECK_INTERVAL_MINUTES`
- `/hermes/prod/REMINDER_DAYS_AHEAD`

## Push Current `.env` Values To Secrets Manager

```bash
cd ~/hermes
./scripts/push_runtime_env_to_secrets_manager.sh hermes/prod/shared .env
```

This creates or updates a single JSON secret containing the same runtime keys as above.

## Deploy On The EC2

```bash
cd ~/hermes
./.venv/bin/pip install -r requirements.txt
sudo systemctl daemon-reload
sudo systemctl restart hermes-bot
sudo systemctl restart slate-checker
```

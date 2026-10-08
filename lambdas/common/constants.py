import os

# General
AWS_DEFAULT_REGION ='us-east-1'
AWS_ACCOUNT_ID = os.environ['AWS_ACCOUNT_ID']
PRODUCT = 'xomper'

# Headers
RESPONSE_HEADERS = {
    "Access-Control-Allow-Origin": "https://xomper.xomware.com",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    "Content-Type": "application/json"
}

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Dynamodb
DYNAMODB_KMS_ALIAS = os.environ['DYNAMODB_KMS_ALIAS']

# Email Service
FROM_EMAIL = os.environ.get('FROM_EMAIL', 'noreply@xomper.xomware.com')
XOMPER_URL = "https://xomper.xomware.com"

# Push Notifications (SNS)
SNS_PLATFORM_APP_ARN = os.environ.get("SNS_PLATFORM_APP_ARN", "")
DEVICE_TOKENS_TABLE = os.environ.get("DEVICE_TOKENS_TABLE", "xomper-device-tokens")

# AI Review (Anthropic / Claude)
AI_REPORTS_TABLE = os.environ.get("AI_REPORTS_TABLE", "xomper-ai-reports")
AI_REVIEW_PROMPT_VERSION = os.environ.get("AI_REVIEW_PROMPT_VERSION", "v1")
AI_REVIEW_POSTDRAFT_PERIOD = "2026"
AI_REVIEW_PRESEASON_PERIOD = "2026-PRESEASON"
AI_REVIEW_PRESEASON_PROMPT_VERSION = os.environ.get(
    "AI_REVIEW_PRESEASON_PROMPT_VERSION", "f2-preseason-2026-05-21"
)
AI_REVIEW_DEFAULT_MODEL = "claude-haiku-4-5"
AI_REVIEW_MAX_TOKENS = 4000
AI_REVIEW_PRESEASON_MAX_TOKENS = 6000
AI_REVIEW_WEEKLY_MAX_TOKENS = 6000
# NFL state.season_type values that mean "regular season has not yet
# started" — used as the preseason pre-flight gate.
AI_REVIEW_PRESEASON_OK_SEASON_TYPES = ("pre", "off")
# NFL state.season_type values that mean "regular season is in
# progress" — used as the weekly recap pre-flight gate. "post" is
# included so playoff weeks (19+) also recap.
AI_REVIEW_WEEKLY_OK_SEASON_TYPES = ("regular", "post")
AI_REVIEW_WEEKLY_PROMPT_VERSION = os.environ.get(
    "AI_REVIEW_WEEKLY_PROMPT_VERSION", "f3-weekly-2026-10-07"
)
AI_REVIEW_WEEKLY_MEMORY_LOOKBACK = 6
AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES = 5
# Set per lambda in Terraform. The CLT cron runs with delivery off: the report
# is stored for clt.dynasty.xomware.com and nobody is emailed or pushed.
AI_REVIEW_WEEKLY_MODEL = os.environ.get("AI_REVIEW_WEEKLY_MODEL", AI_REVIEW_DEFAULT_MODEL)
AI_REVIEW_WEEKLY_DELIVER = os.environ.get("AI_REVIEW_WEEKLY_DELIVER", "true").lower() == "true"

# Week Preview (forward-looking Wednesday newsletter) constants.
# Same OK season types as weekly — preview only fires while the
# regular / post season is active. Same prompt-version env knob so
# we can bump it without a redeploy.
AI_REVIEW_WEEK_PREVIEW_PROMPT_VERSION = os.environ.get(
    "AI_REVIEW_WEEK_PREVIEW_PROMPT_VERSION", "phase2-week-preview-2026-06-02"
)
AI_REVIEW_WEEK_PREVIEW_MAX_TOKENS = 7000
AI_REVIEW_WEEK_PREVIEW_OK_SEASON_TYPES = ("regular", "post")
AI_REVIEW_WEEK_PREVIEW_MEMORY_LOOKBACK = 6
AI_REVIEW_WEEK_PREVIEW_MAX_NEW_MEMORIES = 4

# NFL regular season week count — used by anything that needs to walk
# every regular-season week (WC chain matchups, weekly_recap fallback,
# week_preview). Defined here so notif_weekly_recap + week_preview +
# notif_worldcup_movement all share one source of truth.
TOTAL_REGULAR_WEEKS = 17
AI_MEMORIES_TABLE = os.environ.get("AI_MEMORIES_TABLE", "xomper-ai-memories")

# Admin user id for dry-run delivery (Dominick).
ADMIN_DOMINICK_USER_ID = "594625531702460416"

# LOGO URL
LOGO_URL = f"{XOMPER_URL}/assets/img/xomper-logo.jpg"
BANNER_LOGO_URL = f"{XOMPER_URL}/assets/img/xomper-banner.jpg"

# Admin Portal F5 — CloudWatch Log Viewer.
# Allowlist of log groups the admin CloudWatch tail can read. The key
# is the API-facing slug (what iOS sends + what the IAM policy uses as
# a label); the value is the full CloudWatch log group name. The
# `api_admin_logs_query` lambda validates the inbound `log_group`
# param against these keys as defense-in-depth — the lambda's IAM
# role is also scoped to exactly these 10 ARNs by Terraform.
ADMIN_LOG_GROUP_ALLOWLIST = {
    "ai-review-postdraft": "/aws/lambda/xomper-api-admin-ai-review-postdraft-trigger",
    "ai-review-preseason": "/aws/lambda/xomper-api-admin-ai-review-preseason-trigger",
    "ai-review-weekly": "/aws/lambda/xomper-api-admin-ai-review-weekly-trigger",
    "ai-review-weekly-cron": "/aws/lambda/xomper-notif-ai-review-weekly",
    "weekly-recap": "/aws/lambda/xomper-notif-weekly-recap",
    "email-test": "/aws/lambda/xomper-api-admin-email-test",
    "reports-flag": "/aws/lambda/xomper-api-admin-reports-flag",
    "users-update": "/aws/lambda/xomper-api-admin-users-update",
    "leagues-update": "/aws/lambda/xomper-api-admin-leagues-update",
    "audit-list": "/aws/lambda/xomper-api-admin-audit-list",
}

# Warehouse (Phase 5)
#
# Defaults match the names Terraform creates in xomper-infrastructure, so a
# local run without env vars still points at the real resources rather than
# failing on a KeyError.
WAREHOUSE_BUCKET_NAME = os.environ.get("WAREHOUSE_BUCKET", "xomper-warehouse")
PLAYERS_TABLE_NAME = os.environ.get("PLAYERS_TABLE", "xomper-players")
STATS_TABLE_NAME = os.environ.get("STATS_TABLE", "xomper-stats-current")

# Platform identity (Phase 4.6). Cognito holds credentials; this table holds
# what Cognito does not — which Sleeper account a user has linked.
PLATFORM_USERS_TABLE = os.environ.get("PLATFORM_USERS_TABLE", "xomper-users")

# Followed leagues (Phase 4.8). The inversion of whitelisted_leagues, and the
# cost control: scheduled work iterates followed leagues only.
PLATFORM_FOLLOWS_TABLE = os.environ.get("PLATFORM_FOLLOWS_TABLE", "xomper-follows")

# Social graph. Keyed on the Cognito sub -- Sleeper handles are unverified, so
# a graph built on them would let anyone befriend as someone else.
SOCIAL_TABLE = os.environ.get("SOCIAL_TABLE", "xomper-social")

# CLT Dynasty's roster, keyed by lowercased email. Loaded from Supabase
# whitelisted_users by scripts/migrate_clt_supabase.py; read by clt_gate.
CLT_MEMBERS_TABLE = os.environ.get("CLT_MEMBERS_TABLE", "xomper-whitelisted-users")

# CLT Dynasty's current Sleeper league. Sleeper mints a new id when the league
# renews each season, so this changes once a year, along with the frontend's.
CLT_LEAGUE_ID = "1317249551823814656"
CLT_PROPOSALS_TABLE = os.environ.get("CLT_PROPOSALS_TABLE", "xomper-rule-proposals")
CLT_VOTES_TABLE = os.environ.get("CLT_VOTES_TABLE", "xomper-rule-votes")
CLT_TAXI_TABLE = os.environ.get("CLT_TAXI_TABLE", "xomper-taxi-steal-requests")
CLT_SETTINGS_TABLE = os.environ.get("CLT_SETTINGS_TABLE", "xomper-clt-settings")
CLT_URL = "https://clt.dynasty.xomware.com"

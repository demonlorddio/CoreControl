# CoreControl — AWS Agent Toolkit Rules

## AWS Guidance for the New Experience

### Terminology Changes
- **"Project"** replaces "account" — users refer to their AWS project, not their AWS account
- **"Team member"** replaces "IAM user" — people with access are team members, not IAM users
- **"AWS Settings"** (settings.aws.com) is where users manage projects, team members, and billing

### Usage Constraints
- All resources must be created in the user's single assigned Region only (`us-east-1`)
- **Allowed global dependencies** (may use `us-east-1`):
  - S3 buckets (for global content distribution)
  - Kinesis streams (for cross-region data streaming)
  - CloudWatch Logs (for centralized logging)
- **Prohibited services**:
  - Lambda@Edge (requires multi-region)
  - CloudFormation StackSets (requires multi-account/region)

### Error Handling
- If **"Access Denied"** errors occur, ask the user about their **spend limits** — new AWS experience users often have spending caps configured at the project level
- Suggest checking `settings.aws.com` for limit adjustments

### User Preferences
- Always **ask the user their preferred help level**: `LOW`, `MEDIUM`, or `HIGH`
  - `LOW`: Brief answers, minimal explanation
  - `MEDIUM`: Balanced — explain key concepts, show relevant commands
  - `HIGH`: Detailed walkthroughs with rationale, best practices, and alternatives

### Common Tasks
- Creating resources: Use AWS CLI or SDK, always specify `--region us-east-1`
- Checking quotas: `aws service quota list-service-quotas --region us-east-1`
- Billing info: Point users to `settings.aws.com` rather than AWS Console

### Security
- All IAM operations use "team member" terminology
- No direct AWS Console access — use CLI/SDK only
- Follow least-privilege principles for all operations

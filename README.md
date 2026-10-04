# Jira tickets from templates

Create a project's standard set of Jira Cloud tickets (epics → stories → sub-tasks, with
dependencies) from a YAML file in this repo. It runs on demand in GitHub Actions, so there
is nothing to host and nothing running between projects.

```
create_tickets.py                 the script (Python 3.10+, requests + PyYAML)
config.yaml                       settings for your Jira site (issue types, story-point field, link type)
templates/new-service.yaml        sample template
templates/fragments/*.yaml        reusable pieces, pulled in with `- include:`
.github/workflows/jira-tickets.yml  manual "create" run + template validation on PRs
```

## Setup (one time, about 10 minutes)

1. **Create an automation user** in Jira (recommended over a personal account) with
   *Browse* and *Create issues* permission (plus *Link issues* if you use `depends_on`)
   in the projects it will populate.
2. **Create an API token** for that user at
   <https://id.atlassian.com/manage-profile/security/api-tokens>. Tokens have an expiry
   date, so note when to rotate it.
3. **In the GitHub repo settings** add:
   - Variable `JIRA_BASE_URL`, e.g. `https://your-site.atlassian.net`
   - Secrets `JIRA_EMAIL` and `JIRA_API_TOKEN`

   The `create` job runs in a GitHub environment called `jira` (created automatically on
   first run). To require approval before tickets are created, add required reviewers to
   that environment, and optionally move the two secrets onto it.
4. **Check `config.yaml`** against your site, mainly `story_points_field`: team-managed
   projects use `Story point estimate`, company-managed ones usually `Story Points`.

## Running it

**From GitHub:** Actions → *Jira tickets from template* → *Run workflow*. Fill in the
template name, project key and variables (`service_name=Payments API; start_date=2026-11-02`).
**Dry run is on by default**: the first run shows the plan in the job summary without
creating anything. Untick it to create the tickets.

**Locally:**

```bash
pip install -r requirements.txt
export JIRA_BASE_URL=https://your-site.atlassian.net JIRA_EMAIL=bot@example.com JIRA_API_TOKEN=...

python create_tickets.py --validate --template templates/*.yaml          # offline check
python create_tickets.py --template new-service --project PAY \
    --var service_name="Payments API" --var start_date=2026-11-02 --dry-run
python create_tickets.py --template new-service --project PAY \
    --var service_name="Payments API" --var start_date=2026-11-02
```

`--verbose` prints every issue payload during the dry run.

## How it behaves

- **Preflight first.** Every payload is built and every field name resolved before the first
  write, so config mistakes fail with nothing half-created.
- **Create-only and idempotent.** Each ticket gets two labels: `tpl-<run key>` and
  `tpl-<run key>--<item id>`. Before creating, the script searches for them and skips
  tickets that already exist, so a failed or repeated run fills gaps rather than
  duplicating. Existing tickets are never modified. The run key defaults to the template
  name; pass `--run-key` (or the workflow input) to apply the same template twice to one
  project, e.g. for two services.
- **Ordering.** Epics, then stories (with `parent` = epic), then sub-tasks, then links.
  Each level goes through the bulk-create endpoint in batches of 50, and new keys are
  mapped back to template items via their labels rather than response order.
- **Failures are contained.** If an item fails, its children are skipped and reported, the
  exit code is 1, and re-running after the fix creates only what is missing.
- **Throttling.** 429 responses are retried using `Retry-After` with exponential backoff.
  Writes are never blindly retried on 5xx or timeouts; re-run instead, and the label check
  prevents duplicates.
- **Link direction is verified.** Atlassian's documentation and observed API behaviour
  disagree on which field of `POST /issueLink` is the outward end, so the script checks
  the first link it creates and corrects the orientation if needed.

## Template reference

```yaml
name: new-service                 # default run key; defaults to the file name
variables:
  service_name: {required: true, description: Shown when it is missing}
  start_date: {default: "{{today}}"}     # relative due dates count from start_date
defaults: {labels: [x], components: [y], priority: Medium, assignee: <accountId>, fields: {}}
epics:
  - id: infra                     # required for epics and stories; stable across runs
    summary: "{{service_name}}: infrastructure"   # quote values that start with {{
    description: |                # Markdown subset, converted to Atlassian Document Format
      ## Definition of done
      - [ ] item
    labels: [platform]            # merged with defaults.labels
    components: [Backend]
    priority: High
    assignee: 5b10ac8d82e05b22cc7d4ef5   # Jira accountId
    due: +2w                      # YYYY-MM-DD, or +Nd / +Nw / +Nbd (business days) from start_date
    points: 5                     # goes to config.story_points_field
    type: Task                    # override the default issue type for this level
    fields: {"Team": {"id": "abc"}}  # any other field, by name or id, raw API value
    depends_on: [setup.adr]       # creates "<setup.adr> blocks <infra>"
    stories:
      - id: infra.projects
        summary: Provision projects
        subtasks:
          - Plain string sub-task       # id derived from the summary
          - {id: infra.projects.iam, summary: IAM roles}
  - include: fragments/security-review.yaml
stories: []                       # stories without an epic
```

Built-in variables: `{{project_key}}`, `{{today}}`. Sub-task ids derived from summaries
change when the summary changes, so give sub-tasks an explicit `id` if you plan to edit
their wording after tickets exist.

## Troubleshooting

| Message | Fix |
|---|---|
| `Field 'customfield_…' cannot be set. It is not on the appropriate screen` | Add the field to the project's create screen, or drop it from the template. |
| `No Jira field named 'Story Points'` | Set `story_points_field` in `config.yaml` to the exact name, or the `customfield_…` id. |
| `Several Jira fields are named …` | Use the field id instead of the name. |
| `has no issue type for subtasks` | Enable sub-tasks for the project, or set `issue_types` in `config.yaml`. |
| HTTP 401 / 403 | Check the token and email, and the automation user's project permissions. |
| A re-run created duplicates | Runs within a few seconds of each other can miss each other in search; the workflow's concurrency group prevents overlapping runs per project. |

---
name: drive-filer
description: File email attachments and local project documents into the Chief of Staff Google Drive structure using configurable drive-map.yaml rules.
version: 0.1.0
author: moonlight-lupin
license: Apache-2.0
metadata:
  hermes:
    tags: [drive, filing, google-workspace, documents, chief-of-staff]
    related_skills: [google-workspace, daily-briefing, document-preparer, pipeline-manager]
---

# Drive Filer

## Overview

Drive Filer keeps the company Google Drive clean by filing incoming attachments, generated documents, research outputs, invoices, travel artifacts, and local project files into the numbered Chief of Staff folder structure. Filing decisions are config-driven through `company.yaml` folder IDs and `drive-map.yaml` pattern rules.

All Google Drive calls go through the shared `WorkspaceClient` layer:

```bash
python skills/drive-filer/scripts/drive_file.py search --query "NDA" --max 10
python skills/drive-filer/scripts/drive_file.py upload --file /tmp/report.pdf --parent <folder_id>
python skills/drive-filer/scripts/drive_file.py download --file-id <id> --output /tmp/downloaded.pdf
```

`WorkspaceClient` routes to the workspace provider selected by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365`); the file methods (`files_search`, `files_upload`, `files_download`, `files_trash`) are provider-neutral, so the same commands file into Google Drive or Microsoft 365 (OneDrive / SharePoint). Upload/download use guardrails and return `ActionResult` objects. Filing rules are still resolved by `drive_map.py` which uses config-driven pattern matching.

## When to Use

Trigger this skill on:

- "File this"
- "File this email"
- "Sync to Drive"
- "Put this in the client folder"
- Daily Briefing auto-suggestions for emails with attachments.
- Another Chief of Staff skill producing a document that should be filed.

Do not use it for local-only archiving; Drive Filer's job is Google Drive organization.

## Configuration Sources

### `company.yaml`

```yaml
google:
  account: default
  delegate_email: founder@example.com

drive:
  root_folder_id: "..."
  folders:
    inbox: "..."
    secretarial: "..."
    clients: "..."
    vendors: "..."
    finance: "..."
    templates: "..."
    hr: "..."
    research: "..."
    travel: "..."
    backups: "..."
    knowledge_base: "..."

paths:
  project_root: ~/.hermes/projects/acme/
```

If `drive.folders` is absent, use the root folder ID and paths from the numbered folder structure. If neither IDs nor a root ID exist, stop and ask the user to run onboarding or provide the root Drive folder ID.

### `shared/config/drive-map.yaml`

Drive Filer reads filing rules from `drive-map.yaml` and applies them top-to-bottom. First matching rule wins.

```yaml
filing_rules:
  - pattern: ["NDA", "non-disclosure", "confidentiality"]
    target: "02_Clients/{client}/NDA/"
    fallback: "05_Templates/NDA/"
  - pattern: ["proposal", "quote", "quotation"]
    target: "02_Clients/{client}/Proposals/"
  - pattern: ["SOW", "statement of work", "service agreement", "contract"]
    target: "02_Clients/{client}/Contracts_SOW/"
  - pattern: ["invoice"]
    direction: sent
    target: "04_Finance/Invoices_Sent/"
  - pattern: ["invoice", "bill", "receipt"]
    direction: received
    target: "04_Finance/Invoices_Received/"
  - pattern: ["ACRA", "constitution", "resolution", "register"]
    target: "01_Secretarial/"
  - pattern: ["research", "dossier", "background check"]
    target: "07_Research/"
  - pattern: ["itinerary", "travel", "flight", "hotel"]
    target: "08_Travel/"
  default: "00_Inbox/"
```

Pattern matching uses filename, email subject/body snippet, sender domain, document title, and known pipeline/client metadata. Matching is case-insensitive. Rules may use variables such as `{client}`, `{vendor}`, `{trip}`, `{date}`, and `{deal_id}` when those values are known.

## Drive Folder Structure

Numbered prefixes force Drive to sort in the intended operational order:

```text
Root/                              (company Drive root — ID from company.yaml)
├── 00_Inbox/                      # Unfiled items — reviewed during briefing
├── 01_Secretarial/                # Corporate compliance
│   ├── Constitution/
│   ├── Annual_Returns/
│   ├── Resolutions/
│   └── Registers/
├── 02_Clients/                    # Per-client engagement folders
│   └── {ClientName}/
│       ├── NDA/
│       ├── Proposals/
│       ├── Contracts_SOW/
│       ├── Invoices/
│       ├── Deliverables/
│       └── Correspondence/
├── 03_Vendors/                    # Suppliers, service providers
│   └── {VendorName}/
│       ├── Contracts/
│       ├── Invoices/              # Bills received (AP)
│       └── Correspondence/
├── 04_Finance/                    # Internal accounting
│   ├── Invoices_Sent/             # AR copies
│   ├── Invoices_Received/         # AP copies
│   ├── Bank_Statements/
│   ├── Receipts/
│   └── Tax/
├── 05_Templates/                  # Reusable document templates
│   ├── NDA/
│   ├── Proposals/
│   ├── Contracts_SOW/
│   ├── Invoices/
│   └── TandCs/
├── 06_HR/                         # If applicable
│   ├── Employment_Contracts/
│   ├── CPF_IR8A/
│   └── Policies/
├── 07_Research/                   # Deep research + entity dossiers
│   ├── Market_Research/
│   ├── Entity_Dossiers/
│   └── Competitive_Intel/
├── 08_Travel/                     # Itineraries + receipts
│   └── {TripName}/
├── 09_Backups/                    # Hermes backups
│   ├── config/
│   ├── skills/
│   ├── wiki/
│   └── data/
└── 10_Knowledge_Base/             # Wiki/2nd brain exports
    ├── Entities/
    ├── Concepts/
    └── Comparisons/
```

## Workspace Access

Drive Filer's intent is: search the file store, ensure/create folders, and upload/download files. Prefer the `drive_file.py` wrapper (shown in the Overview) — it routes through `WorkspaceClient` and applies guardrails/audit. Normalize files you read or report to the canonical `file` shape in `shared/scripts/schemas.py` (`{id, name, mime_type?, modified?, link?, parents?, source?}`).

If you access the file store directly instead of through the wrapper, use the first available path in this order:

1. **Native connector tools** in the agent's environment — the Google Drive connector, or the Microsoft 365 OneDrive / SharePoint connector.
2. **The configured workspace provider** via `shared/scripts/workspace_client.py`: `get_workspace_client(config).files_search(query, max_results=...)`, `.files_upload(file_path, parent_id=...)`, `.files_download(file_id, output_path)`, `.files_trash(file_id)`. The provider is chosen by `integrations.workspace.provider` in `company.yaml` (`google_api` | `composio` | `m365`).

Search queries in these examples use the Google Drive query dialect; the `m365` provider translates the same intent to Microsoft Graph, and native connectors accept natural-language/structured queries. To fetch a mail attachment before filing, obtain it through an approved mail access path (a Gmail/Outlook connector or `workspace_client` mail methods), then return to Drive Filer for classification and upload.

## Workflow A — File Email Attachment

1. Identify the target email by user reference, Gmail search result, or Daily Briefing item.
2. Confirm which attachment(s) to file if there is more than one.
3. Download attachments through an approved mail access path (a Gmail/Outlook connector or `workspace_client` mail methods).
4. Build filing context:
   - filename,
   - email subject,
   - sender and domain,
   - direction (`received` unless generated/sent by the company),
   - known client/vendor from Pipeline Manager or user confirmation.
5. Apply `drive-map.yaml` rules top-to-bottom.
6. Resolve variables like `{client}`. If required variables are missing and a fallback exists, use fallback; otherwise ask once.
7. Ensure the Drive folder exists, creating missing subfolders under the configured root when safe.
8. Upload the file through an approved workspace access path (`drive_file.py upload` / `files_upload`).
9. Optionally mark the email as read only after upload succeeds and the user has allowed it.
10. Return the Drive link, final folder, and any rule used.

Completion criterion: every requested attachment is either uploaded with a Drive link or reported with a specific blocking reason.

## Workflow B — Sync Local Files

1. Read `paths.project_root` from `company.yaml` or use the user-specified directory.
2. Scan files under the requested local directory, excluding temporary files (`~$*`, `.DS_Store`, `.git/`, `__pycache__/`, logs) unless the user explicitly asks.
3. For each file, classify with `drive-map.yaml` and known metadata.
4. Search target Drive folder for the same name and size/checksum metadata if available.
5. Upload new or changed files only; skip exact duplicates.
6. Report uploaded count, skipped count, failed count, and target folders.

Completion criterion: sync report accounts for every scanned file.

## Workflow C — Auto-Suggest During Daily Briefing

When Daily Briefing sees emails with attachments, Drive Filer should produce suggestions, not take action:

```text
📎 {filename} from {sender} — suggested folder: {folder}. File it? (yes/no)
```

If the user confirms, execute Workflow A. If the user says no, leave the item in `00_Inbox/` or do nothing depending on whether the attachment was already downloaded.

## Filing Rule Design

A filing rule may include:

| Key | Meaning |
|---|---|
| `pattern` | List of strings/regex-like terms matched case-insensitively. |
| `direction` | Optional `sent` or `received`. |
| `sender_domain` | Optional domain constraint such as `iras.gov.sg`. |
| `client_required` | If true, ask for `{client}` rather than fallback. |
| `target` | Target Drive path relative to root. |
| `fallback` | Safe fallback when variable substitution cannot be completed. |
| `tags` | Optional tags to write into a filing log. |

Maintain a local filing log in the company project if available:

```yaml
filed_documents:
  - date: 2026-07-09
    source: gmail
    filename: NDA_Acme.pdf
    target: 02_Clients/Acme/NDA/
    drive_file_id: ...
    rule: NDA
```

## Integrations

- **Daily Briefing:** Auto-suggest filing for inbox attachments.
- **Document Preparer:** File generated DOCX/PDF to client or templates folders.
- **Self-Sign:** File signed PDFs into contracts/SOW or client folders.
- **Pipeline Manager:** Supplies client/deal metadata and receives document links.
- **Bookkeeper:** Files invoices and receipts to finance folders.
- **Backup:** Uses `09_Backups/` as the upload destination.

## Common Pitfalls

1. **Filing without metadata.** If `{client}` or `{vendor}` is needed and unknown, ask or use an explicit fallback.
2. **Creating duplicate folders with spelling variants.** Search existing siblings before creating `Acme`, `ACME`, or `Acme Corp`.
3. **Marking email read before upload succeeds.** Only mark read after Drive confirms upload.
4. **Bypassing the workspace layer.** All file operations go through an approved workspace access path (`drive_file.py` / `WorkspaceClient` / connector tools), never a hard-coded vendor API.
5. **Over-filing from Daily Briefing.** Briefing mode suggests; user confirmation triggers action.

## Verification Checklist

- [ ] Loaded `company.yaml` folder IDs and `drive-map.yaml` rules.
- [ ] Used an approved workspace access path (`drive_file.py`, connector tools, or `workspace_client`) for file-store calls.
- [ ] Rule match, fallback, and variables were reported.
- [ ] Duplicate check ran before upload during sync.
- [ ] Final response includes Drive links or precise failure reasons.

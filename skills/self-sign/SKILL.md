---
name: self-sign
description: Detect signature blocks in PDF or DOCX documents, confirm the correct party blocks with the user, and place the user's prepared signature without external e-sign services.
version: 0.1.0
author: Phronesis Applied
license: MIT
metadata:
  hermes:
    tags: [signature, pdf, docx, documents, offline, chief-of-staff]
    related_skills: [document-preparer, drive-filer, daily-briefing, pipeline-manager, weekly-review]
---

# Self-Sign

## Overview

Self-Sign handles the common case where the user needs to place their own signature on a received or generated document. It scans PDFs and DOCX files for signature locations, extracts nearby party context, matches likely user-owned blocks against `company.yaml`, asks the user to confirm, then places a prepared signature image.

It does not collect signatures from third parties and does not use DocuSeal or any external signing service. Phase 1 is pure Python:

- `pymupdf` / `fitz` for PDF scanning and PDF signature placement.
- `python-docx` for DOCX scanning and template-generated documents.

## When to Use

Use this skill when:

- The user says "sign this", "self-sign", "add my signature", or "review documents for signature".
- Daily Briefing finds emails with "please sign", "for signature", or similar plus attachments.
- Document Preparer generates a document where the user's company is a signatory.
- Pipeline Manager reaches a stage that requires the user to execute a document.

Do not use this for sending documents to other parties for signature. That is a separate Phase 2 e-sign sender workflow.

## Configuration

Read `self_sign` and identity fields from `company.yaml`:

```yaml
company:
  name: "Phronesis Applied Pte Ltd"
  business_type: professional_services

self_sign:
  signature_image: "shared/assets/signature.png"
  initials_image: "shared/assets/initials.png"
  company_stamp: "shared/assets/stamp.png"
  auto_date: true
  output_format: pdf
  party_aliases:
    - "Service Provider"
    - "Consultant"
    - "Contractor"
    - "The Company"
  detection_patterns:
    signature:
      - "Signature:"
      - "Signed by:"
      - "Sign here:"
      - "Authorised Signatory:"
      - "For and on behalf of"
      - "______________"
    date:
      - "Date:"
      - "Date Signed:"
      - "Dated this"
    party:
      - "For and on behalf of"
      - "Party A"
      - "Party B"
      - "First Party"
      - "Second Party"
      - "The Client"
      - "The Service Provider"
      - "The Consultant"
      - "Lessor"
      - "Lessee"
      - "Buyer"
      - "Seller"
      - "Employer"
      - "Employee"
```

Defaults:

- `auto_date`: true.
- `output_format`: pdf.
- `party_aliases`: service-provider/consultant/contractor aliases for `professional_services` businesses.
- `detection_patterns`: built-in signature/date/party patterns from the script.

## Signature Detection

Use the bundled detector:

```bash
python /root/.hermes/plugins/chief-of-staff/skills/self-sign/scripts/sign_detector.py \
  /path/to/document.pdf --format json \
  --company "{company.name}" --alias "Service Provider" --alias "Consultant"
```

The detector returns a JSON list of `SignatureLocation` objects:

```json
{
  "page": 4,
  "paragraph": null,
  "coordinates": [72.0, 620.0, 260.0, 638.0],
  "matched_text": "Signature: __________________",
  "party_context": "For and on behalf of the Service Provider: Phronesis Applied Pte Ltd",
  "confidence": 0.91,
  "location_type": "signature",
  "matched_party": "self"
}
```

### PDF Detection Rules

For PDFs, `sign_detector.py`:

1. Extracts text with coordinates using PyMuPDF.
2. Searches for configured signature patterns.
3. Finds underscore runs of at least 10 characters.
4. Scans upward on the page for party labels such as "For and on behalf of", "Party A", "The Client", and "The Service Provider".
5. Returns page number, bounding box, matched text, party context, and confidence.

### DOCX Detection Rules

For DOCX, `sign_detector.py`:

1. Iterates document paragraphs and table-cell paragraphs.
2. Searches for signature/date patterns and underscore runs.
3. Tracks paragraph index as the location.
4. Scans preceding paragraphs for party context.
5. Returns paragraph location, matched text, party context, and confidence.

## Multi-Party Handling

Documents often contain multiple blocks. Self-Sign must detect all of them and only sign the user's block after confirmation.

Party identification strategy:

1. Detect all signature and date blocks.
2. For each block, scan nearby text for party labels:
   - "For and on behalf of [Party]"
   - "Signed by the [Client]"
   - "The Service Provider"
   - "Party A" / "Party B"
   - "Lessor" / "Lessee"
   - "Buyer" / "Seller"
   - "Employer" / "Employee"
3. Match against the user's identity:
   - exact or normalized match to `company.name`,
   - aliases from `self_sign.party_aliases`,
   - professional-services defaults: Service Provider, Consultant, Contractor.
4. Present all blocks to the user with labels and recommendation.
5. Sign only confirmed blocks. If no reliable match exists, present all blocks neutrally.

User confirmation prompt shape:

```text
📝 Found 3 signature locations in Service_Agreement.pdf:

1. Page 4 — "For and on behalf of the Client:"
   Party context: Beta Corp / The Client
   Recommendation: likely NOT you.
   Sign here? (yes/no)

2. Page 4 — "For and on behalf of the Service Provider:"
   Party context: Phronesis Applied Pte Ltd / Service Provider
   Recommendation: likely YOU.
   Sign here? (yes/no)

3. Page 4 — "Date: ______________" near Service Provider block
   Fill date? (yes/no)
```

## Workflow A — Sign a Received Document

1. Get the document from a local path, Drive file, or Gmail attachment.
2. Confirm `self_sign.signature_image` exists and is a readable image.
3. Run `sign_detector.py` and parse JSON output.
4. If no locations are found, report that clearly and offer manual placement guidance.
5. Present all detected locations with party context and confidence.
6. Ask the user to confirm which signature/date fields to fill.
7. Insert the signature image at confirmed locations. For PDF, place the image within or just above the detected line bounding box, scaled to fit line width.
8. If `auto_date` is true and a confirmed date field exists, insert today's date in the configured locale.
9. Save as `{original_stem}_signed.pdf`.
10. Offer to file the signed document through Drive Filer.

Completion criterion: output file exists, user knows which blocks were signed, and Drive filing is offered.

## Workflow B — Sign a Generated Document

1. Document Preparer generates a `.docx` from a registered template.
2. Inspect template metadata and document content to determine whether the user's company is a signatory.
3. Convert to PDF if a converter is available; otherwise scan DOCX and inform the user of the output limitation.
4. Run detection, present blocks, and sign confirmed locations.
5. Save signed PDF and return it to Document Preparer for filing or delivery.

Completion criterion: generated document has either been signed or explicitly skipped by user choice.

## Workflow C — Bulk Review During Daily Briefing

1. Daily Briefing searches unread/recent mail for signing phrases and attachments.
2. Present each candidate:

```text
📎 {filename} from {sender} — appears to need your signature. Review now?
```

3. On confirmation, run Workflow A.
4. Record signed documents for Weekly Review.

Completion criterion: each candidate is reviewed, deferred, or dismissed.

## Onboarding: Signature Assets

During onboarding:

1. Ask whether the user has a transparent PNG signature image.
2. If yes, copy it to `shared/assets/signature.png` and update `company.yaml`.
3. If no, explain a safe creation path: sign on white paper, photograph/scan, remove background, store locally.
4. Ask whether initials and company stamp/chop images should be added.
5. Validate the signature image by opening it and confirming non-zero dimensions.
6. Never upload signature assets to third-party services from this skill.

## Integrations

| Skill | Integration |
|---|---|
| Daily Briefing | Flags candidate unsigned documents in inbox. |
| Document Preparer | Offers signing immediately after generated contracts, NDAs, SOWs, and invoices if appropriate. |
| Pipeline Manager | Tracks documents that must be signed or have been executed. |
| Drive Filer | Files signed PDFs to the correct client/vendor/finance folder. |
| Weekly Review | Reports documents signed this week and pending signature backlog. |

## Common Pitfalls

1. **Signing the wrong party block.** Always present all detected blocks and require user confirmation.
2. **Assuming Service Provider always means user.** It is a useful hint for professional-services configs, not automatic permission.
3. **No signature image.** Stop and run onboarding; do not synthesize a fake signature.
4. **DOCX legal finality.** Prefer signed PDF output for legal documents; DOCX remains editable.
5. **Missing PyMuPDF.** The detector handles this gracefully; report dependency installation rather than crashing.

## Verification Checklist

- [ ] `company.yaml` identity and `self_sign` config were read.
- [ ] Signature image exists before signing.
- [ ] Detection output includes all signature candidates, not only the likely user block.
- [ ] User confirmed every signed location.
- [ ] Output saved as PDF where possible.
- [ ] Signed file offered to Drive Filer and logged for Weekly Review.

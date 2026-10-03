# ISO 20022 Validator

Validates ISO 20022 payment messages and reports errors with line number, XML path and schema message.
Built for QA engineers, fintechs and banks testing payment message flows.

**Status: v0.5** – `pain.001` validation (`pain.001.001.03` and `pain.001.001.09`): XSD schema **and business rules**; an edit-and-revalidate **Test** workspace; message generation from a template; batch test-suite generation (zip + manifest); **simulated bank replies** (`pain.002` status reports); CLI + Web UI.

## Usage

```bash
pip install -e ".[dev]"

iso20022-validate samples/pain.001/pain.001.001.09/valid_single_payment.xml   # exit 0
python -m iso20022_validator.cli <file.xml>                    # same, if the script is not on PATH
python -m streamlit run src/iso20022_validator/app.py          # web UI
pytest
```

Output always names the schema that was used. `VALID` means the message passed **both** the schema and the
business rules:

```
Schema: pain.001.001.03
VALID
```

Every error is tagged with the layer it comes from (`schema` or `business/<rule>`):

```
Schema: pain.001.001.09
INVALID (1 error)
  [business/ctrl_sum] line 8 /Document/CstmrCdtTrfInitn/GrpHdr/CtrlSum: CtrlSum is 1200.75 but transactions sum to 1250.50
```

Exit code is `0` for valid, `1` for invalid (also for non-well-formed XML, unsupported namespaces or unreadable files).

## Scope

### Validation (v0.1)

- Validate `pain.001` messages against the ISO 20022 XSD.
- **The schema version is detected automatically** from the namespace of the root `<Document>` element
  (e.g. `urn:iso:std:iso:20022:tech:xsd:pain.001.001.03`) and the matching XSD is used.
  An unknown or missing namespace yields a clear `Unsupported namespace '…'` error listing the supported versions.
- Each error shows line number, XML path and the schema message.
- CLI and Streamlit UI both show the detected schema version.
- The Web UI validates an uploaded file ("Upload file") or XML typed/pasted into the editor ("Test"); both go through
  the same `validate_bytes` call.

Supported versions: `pain.001.001.03`, `pain.001.001.09`.

### Business rules (v0.4)

A second layer on top of the XSD, for `.03` and `.09`. **The XSD runs first; business rules run only on a
schema-valid message** (they read elements the schema has guaranteed to exist), and the output says when they were
skipped. A message is reported `VALID` only if it passes every layer that applies.

| Rule (`rule` field) | What is checked |
|---|---|
| `ctrl_sum` | `CtrlSum` in `GrpHdr` and in each `PmtInf` (if present) equals the sum of the `InstdAmt` of its transactions. A difference below half a cent counts as equal (decimal rounding). |
| `nb_of_txs` | `NbOfTxs` in `GrpHdr` and in each `PmtInf` (if present) equals the number of `CdtTrfTxInf` it covers. |
| `iban_checksum` | every `IBAN` passes the ISO 13616 mod-97 checksum (the XSD only checks the format). |
| `execution_date_past` | `ReqdExctnDt` of each `PmtInf` is not before today (`<Dt>` or `<DtTm>` in `.09`). |

Violations are reported like schema errors (line number, XML path, clear message, e.g. `CtrlSum is 1250.75 but
transactions sum to 1200.75`) plus a **type**: `kind` is `schema` or `business`, and business errors carry the `rule`.
The CLI prints it as `[business/ctrl_sum]`; the Web UI shows it in each error's header (see *Error list*).

Limits: IBAN length per country is not checked (only the checksum); amounts of different currencies are summed
as plain numbers; only `InstdAmt` (or `EqvtAmt/Amt`) is summed; BIC checks are not implemented. The date rule uses the
real current date (`validate_bytes(..., today=...)` overrides it, e.g. for tests), so a message that is valid today
becomes invalid once its execution date has passed.

### Error list (all modes)

Errors are shown as **one readable block each** (not a fixed-width table row, which cut long messages off): a header
with the type (`schema`, `business`, `xml`, `namespace`), the rule for business errors and the XML path, then the
**whole message, wrapped**, so nothing needs a wider window (checked at 1200, 620 and 380 px). The same list is used by
Upload, Test, Generate and Batch Generate.

- In **Test**, each error's line number is a button that jumps the editor to that line.
- In **Upload**, the button **opens the file in the Test editor**, already validated, at that line.
- In Generate and Batch Generate there is no editor to jump to, so the line is plain text.

The jump needs a small script because `streamlit-ace` has no "go to line" option: it finds the live Ace instance in
the page and uses Ace's own `gotoLine`/`scrollCursorIntoView`. Checked in a real browser (Edge, headless) by
`tests/test_e2e_navigation.py`: the cursor lands on the table's line, that line is on screen, typing afterwards is not
pulled back, and the message is fully visible at narrow widths. That test is skipped when Playwright or a
Chromium-based browser is missing; the Python side (the right line is handed over, once) is covered by
`tests/test_error_list.py`.

### Test mode (v0.4)

The Web UI's **Test** mode (formerly "Paste XML") is an edit-and-revalidate workspace:

- A code editor ([Ace](https://ace.c9.io/) via `streamlit-ace`) with **XML syntax highlighting and line numbers**.
- **Nothing is validated while you type.** Click **Validate**; edit; click **Validate** again. When the text has
  changed since the last validation, the old result is hidden and a note asks you to validate again.
- Validation is the same path as file upload (`validate_bytes`): **schema and business rules**, same error list
  (see below) and detected schema version.
- **Line numbers in the list are the editor's line numbers**: the error lines also get a ✖ marker in the editor gutter
  (removed as soon as you edit the text). **Click an error's "Line N" button and the editor jumps to that line**
  (cursor placed, line scrolled to mid-height and flashed). Validate button turns green for a valid message and red
  for an invalid one.
- The editor content is **kept** between validations and when you switch to another mode and back.

### Message generation (v0.2)

The Web UI's **Generate** mode creates a new, schema-valid message from a valid pain.001 template:

1. Upload a pain.001 XML (`.03` or `.09`) as a template. It must be schema-valid; otherwise it is refused and its
   validation errors are shown. (Business rules are not required of a template, so an old template whose date has
   passed is still usable.)
2. The form is pre-filled from the template: Message ID (`MsgId`), payment reference (`EndToEndId`), debtor name and
   IBAN, creditor name and IBAN, amount, currency and requested execution date.
3. **`MsgId` and `EndToEndId` are generated by default** (`MSG-{YYYYMMDD-HHMMSS}-{4 random A-Z/0-9}`, and the same
   pattern with `E2E`), so messages generated from one template do not collide. Both stay editable; a value you typed is
   used as typed. After a message is generated, unedited IDs are re-rolled for the next one.
4. On **Generate**, only those field values are replaced in the template, and `CtrlSum` is recomputed from the
   amounts so an edited amount does not make the message inconsistent. Everything else (structure, other elements,
   schema version, XML declaration) is written back as it was; generating without edits reproduces a consistent
   template byte for byte.
5. The result is run through the validator, **schema and business rules**. If it is valid, the XML is shown and
   offered for download; if not (e.g. a malformed IBAN, a wrong IBAN checksum, an execution date in the past), the
   validation errors are shown and **no file is produced**.

Notes and limits:

- Only the **first** `CdtTrfTxInf` of the first `PmtInf` is edited; with several transactions the UI says so
  (multi-transaction editing comes later).
- Templates must use `IBAN` accounts, `InstdAmt` and a single requested execution date (`<Dt>` in `.09`);
  otherwise the template is rejected with a message naming what is missing.
- `NbOfTxs` is never changed (only the first transaction is edited); `CtrlSum` always follows the amounts.
- The same logic is available as a library: `read_template(bytes)` and `generate(bytes, MessageFields)` in
  `iso20022_validator.core`.

### Batch test-suite generation (v0.3)

The Web UI's **Batch Generate** mode creates many separate messages at once, valid and deliberately invalid, as one
downloadable `.zip` with a manifest:

1. Upload a valid pain.001 template (`.03` or `.09`) and edit the same fields as in Generate (debtor, creditor, amount,
   currency, date). `MsgId` and `EndToEndId` are not in this form: every file gets its own.
2. Choose the **number of files** (1–100) and the **valid share** (0–100 %, default 80 % valid / 20 % invalid).
   The number of invalid files is `count × (100 − valid %)`, rounded half up.
3. **Valid files** vary the amount (0.1× to 2× the base amount, two decimals) and get a fresh, unique `MsgId` and
   `EndToEndId` (same generator as single messages). `CtrlSum` is recomputed per file, so valid files pass the
   business rules too (they need a base execution date that is not in the past).
4. **Invalid files** are valid files with **exactly one injected error**, taken from a fixed list of error types
   (a shuffled bag, so every type is used before any repeats):

   | Error type | What is injected |
   |---|---|
   | `invalid_iban` | debtor or creditor IBAN with letters in the check digits, a lowercase country code, or spaces |
   | `invalid_amount` | negative, non-numeric or comma-decimal amount |
   | `missing_field` | `MsgId` or `EndToEndId` removed |
   | `malformed_date` | wrong format (`05/10/2026`, `20261005`, `2026-10-5`) or impossible date (`2026-13-01`, `2026-02-30`) |

   **Every file is run back through the validator before it is packaged.** An invalid file that turns out to be valid
   is never shipped (the batch fails instead), so the manifest cannot mislabel a file.
5. The zip contains the XML files (`001_valid.xml`, `002_invalid_iban.xml`, …) and `manifest.csv` with one row per
   file: `filename`, `status` (`valid`/`invalid`), `error_type` (empty for valid files), `detail` (what exactly was
   changed) and `schema_version`.

Adding an error type means appending an `ErrorType(name, description, inject)` to `ERROR_TYPES` in
`core/batch.py`; nothing else changes. The same logic is available as a library:
`generate_batch(template, base_fields, count=…, valid_percent=…, seed=…)` (a `seed` makes amounts, positions and error
choices reproducible; IDs are always fresh).

Limits: only the first transaction of a template is varied. The injected errors are all **schema-level** (so each
invalid file has exactly one error); business-rule errors (wrong `CtrlSum`, bad IBAN checksum, past date) are not
injected yet.

### Simulated bank reply (v0.5)

The Web UI's **Simulate** mode closes the loop from "validate a message" to "see the bank's answer": it takes a valid
`pain.001` and builds the matching `pain.002` (Customer Payment Status Report) with a status you choose per transaction.

1. Upload a pain.001 (`.03` or `.09`). It must pass the **schema and the business rules**; otherwise its errors are shown
   and nothing else happens (like Generate with an invalid template).
2. The original's `GrpHdr/MsgId` and `CreDtTm`, and per transaction `PmtInfId`, `EndToEndId` and (if present)
   `InstrId` (and `UETR` for `.09`) are read and every transaction is listed.
3. Each transaction has a status selector, **Accepted (ACSC) by default**, so the happy path is one click:
   **Rejected (RJCT)** with a reason-code dropdown (`AC01` Incorrect account number, `AM04` Insufficient funds, `RC01` Bank
   identifier incorrect, `MS03` Reason not specified, plus `AC04`, `AC06`, `AG01`, `AM05`, `BE01`, `DT01`), or **Pending (PDNG)**.
4. **Generate pain.002** builds the reply: a new `GrpHdr` (`MsgId` `STS-{YYYYMMDD-HHMMSS}-{4 random A-Z/0-9}`, new `CreDtTm`);
   `OrgnlGrpInfAndSts` with the original `MsgId`, the original message name (`pain.001.001.03` / `.09`), its `CreDtTm`,
   `NbOfTxs` and `CtrlSum`, a group status and the number of transactions per status; one `OrgnlPmtInfAndSts` per original
   `PmtInf`; and one `TxInfAndSts` per transaction with `OrgnlEndToEndId` (and `OrgnlInstrId`, `OrgnlUETR` when the original
   has them), `TxSts`, and for a rejection `StsRsnInf/Rsn/Cd` with the reason code.
5. **The reply is validated against its own XSD before it is offered** (the same safety net as Generate). If it were
   invalid, the errors are shown and **no file is produced**.

The group status (and each `PmtInfSts`) is derived: all transactions alike -> that status; a mix -> `PART`.

#### Which pain.002 answers which pain.001

| pain.001 (request) | pain.002 (reply) | Schema generator build (both files carry the same header) |
|---|---|---|
| `pain.001.001.03` | `pain.002.001.03` | SWIFTStandards Workstation R6.1.0.2, 2009 Jan 08 17:30:53 |
| `pain.001.001.09` | `pain.002.001.10` | Standards Editor R1.6.15, 2019 Feb 14 11:57:59 |

ISO 20022 numbers versions per message family, so the numbers differ (`.09` vs `.10`) although the messages belong
together. The pairing is taken from the implementation guidelines of the schemes that use these messages, not guessed:

- **`.03` with `.03`**: the EPC SEPA Credit Transfer Customer-to-Bank Implementation Guidelines, version 8.0 (25 Nov 2014,
  "version 2009 of the ISO 20022 XML message standards") list *"Use of the Customer Credit Transfer Initiation
  (pain.001.001.03)"* and *"Use of the Customer Payment Status Report (pain.002.001.03)"*, and the status report's
  *Original Message Name Identification* is `pain.001.001.03`
  ([EPC132-08 v8.0](https://www.europeanpaymentscouncil.eu/sites/default/files/KB/files/EPC132-08%20C2B%20CTIG%20V8.0%20Approved.pdf)).
- **`.09` with `.10`**: the 2025 EPC Customer-to-PSP Implementation Guidelines specify *"Customer Credit Transfer Initiation
  (pain.001.001.09)"* and *"Customer Payment Status Report (pain.002.001.10)"*, in both the SEPA Credit Transfer
  ([EPC132-08 2025 v1.0](https://www.europeanpaymentscouncil.eu/sites/default/files/kb/file/2024-11/EPC132-08%20SCT%20C2PSP%20IG%202025%20V1.0.pdf)) and the SEPA Instant Credit Transfer
  ([EPC121-16 2025 v1.0](https://www.europeanpaymentscouncil.eu/sites/default/files/kb/file/2024-11/EPC121-16%20SCT%20Inst%20C2PSP%20IG%202025%20V1.0.pdf)) editions (section 2.2.1); a CBPR+ overview says the same:
  *"pain.001.001.09 is a Customer Credit Transfer Initiation message. The response to this message is pain.002.001.10"*
  ([iso20022payments.com](https://www.iso20022payments.com/cbpr/pain-001-pain-002/)).
- Both pairs also share their release: the two XSDs of each pair were produced by the same generator build on the same
  timestamp (see the table), i.e. they come from the same ISO 20022 maintenance release.

The EPC PDFs were checked by extracting their text (the version identifiers above are quoted from it); the CBPR+ page is a
secondary source and agrees. Other pairings (e.g. newer `pain.002` versions) are not supported.

Limits: replies carry statuses only (no charges, tracking data or `OrgnlTxRef` details); `pain.002` is read-only input
for validation elsewhere (Upload/Test accept it and check the **schema only**, since there are no business rules for it yet).

Out of scope (later): BIC checks, scheme rules (CBPR+, Fedwire, SEPA), AI explanations, other message types
(pacs.008, pacs.009, camt.*), multi-transaction editing.

## Architecture

1. **Schema validation (XSD)** – is the XML structurally valid? (v0.1)
2. **Business rules** – are the values consistent? (v0.4) Separate module (`core/rules.py`), run only if layer 1 passes.
3. **Explanations** – what is wrong and how to fix it (later)

`engine.validate_bytes` runs layer 1 then layer 2; `engine.validate_schema` is layer 1 alone. Message generation
(v0.2) and batch test-suite generation (v0.3) sit beside these layers: they edit a template and always finish by
validating the result through both.

**Respond** (v0.5) is a third capability beside Generate and Batch Generate: instead of editing a request, it builds the
*reply* to one (`core/simulate.py`). It reuses the same safety net, validating the generated `pain.002` against its own
XSD before returning it, and the engine's plugin discovery picks up the `pain.002` schemas like any other folder in
`messages/`. Business rules are tied to message families (`engine.BUSINESS_RULE_FAMILIES`): `pain.001` gets schema + rules,
`pain.002` the schema only, and a result says which applies (`business_rules_apply`).

The validation and generation logic is a standalone library; the CLI and Web UI only call it.

Each schema version is a plugin: a folder `messages/<id>/schema.xsd`. The engine discovers plugins by reading each
XSD's `targetNamespace` and picks the one matching the document's root namespace, so **adding a version means adding a
folder – the core does not change.**

```
src/iso20022_validator/
├── core/                    # engine (namespace detection, XSD + orchestration), rules (business rules),
│                            # generator (templates), batch (test suites, error types),
│                            # simulate (pain.001 -> pain.002 reply), error model
├── messages/
│   ├── pain_001_001_03/schema.xsd
│   ├── pain_001_001_09/schema.xsd
│   ├── pain_002_001_03/schema.xsd   # reply to pain.001.001.03
│   └── pain_002_001_10/schema.xsd   # reply to pain.001.001.09
├── cli.py
├── editor.py                # the Ace XML editor used by the Test mode
└── app.py                   # Streamlit: Upload file / Test / Generate / Batch Generate / Simulate
samples/
└── pain.001/
    ├── pain.001.001.03/     # valid and invalid examples (+ unsupported namespace); valid_*.xml double as templates
    └── pain.001.001.09/     # valid and invalid examples; valid_two_payments.xml is a 2-transaction template
                             # valid_three_payments.xml (both folders): 2 PmtInf, 3 transactions, InstrId on two,
                             # UETR in .09: the input for the Simulate tests
                             # invalid_{ctrlsum_mismatch,nboftxs_mismatch,iban_checksum,past_date}.xml are
                             # schema-valid but break one business rule each (in both folders)
tests/
```

## Acceptance criteria

- [x] A valid pain.001 message (.03 or .09) returns VALID, names the schema, exit code 0
- [x] A missing mandatory element (e.g. MsgId) is reported with line number and XML path
- [x] A wrong data type (e.g. text in an amount) is reported
- [x] An unknown element is reported
- [x] A file that is not well-formed XML gives a clear error, not a crash
- [x] The schema version is detected from the root namespace and shown by CLI and Web UI
- [x] An unsupported namespace gives a clear error naming it
- [x] The Web UI uses the same library call as the CLI
- [x] All cases are covered by pytest tests using files in `samples/`

Business rules:

- [x] `CtrlSum` (`GrpHdr` and each `PmtInf`) must equal the sum of its transaction amounts, within decimal rounding
- [x] `NbOfTxs` (`GrpHdr` and each `PmtInf`) must equal the actual number of `CdtTrfTxInf`
- [x] Every IBAN must pass the mod-97 checksum, not just match the format
- [x] `ReqdExctnDt` must not be in the past
- [x] Violations are reported with line, XML path and a clear message (`CtrlSum is 1250.75 but transactions sum to 1200.75`)
- [x] Errors carry a type (`schema` / `business`) and, for business errors, the rule; shown by CLI and Web UI
- [x] Business rules run only after the XSD passes; a message is never reported valid if either layer fails
- [x] Each rule has a violating and a satisfying test case on both `.03` and `.09`

Test mode:

- [x] The "Paste XML" mode is renamed "Test" and uses a code editor with XML highlighting and line numbers
- [x] Validation is on demand (Validate button), never on every keystroke, and covers schema + business rules
- [x] The error table's line numbers match the editor's lines (and are marked in the editor gutter)
- [x] The editor content is preserved between validations and when switching modes
- [x] Clicking an error's line number jumps the editor to that line (verified in a real browser)
- [x] The full error text is readable without resizing the window: no truncation, wrapped, same list in every mode
- [x] In Upload mode the line button opens the file in the Test editor at that line

Message generation:

- [x] Uploading a valid `.03` or `.09` template pre-fills the form with its current values
- [x] Editing the fields produces a schema-valid message of the same schema version as the template
- [x] Only the edited fields differ from the template; the rest of the XML is unchanged
- [x] An invalid value (malformed IBAN, wrong IBAN checksum, non-numeric amount, bad currency, date or past date,
      empty `MsgId`) is caught by the validator and shown as errors; no download is offered
- [x] `CtrlSum` follows an edited amount, so a generated message passes the business rules
- [x] An invalid or unsupported template is refused with its errors
- [x] `MsgId` and `EndToEndId` are generated by default, are editable, and two messages generated from the same
      template get different values
- [x] With several transactions the first is edited and the UI says so

Batch test-suite generation:

- [x] Generating N files (1–100) puts N XML files plus `manifest.csv` in the zip
- [x] The valid/invalid ratio is respected (default 80/20, within rounding)
- [x] Every valid file passes the validator; every invalid file fails it with exactly one error of the stated kind
- [x] Valid files have unique `MsgId`/`EndToEndId` and varied amounts; invalid files carry one of the error types
      (invalid IBAN, negative or non-numeric amount, missing mandatory field, malformed date)
- [x] `manifest.csv` lists every file with its status and, for invalid files, the injected error type
- [x] Values that cannot make a valid message are refused (no zip); error types are easy to extend

Simulate (pain.001 -> pain.002):

- [x] The pain.001 -> pain.002 pairing is `.03 -> .03` and `.09 -> .10`, with sources (see above)
- [x] An invalid pain.001 (schema or business rules) shows its errors and nothing is simulated
- [x] The original `MsgId`, `CreDtTm`, `PmtInfId`, `EndToEndId` (and `InstrId` if present) are read and every transaction is listed
- [x] Every transaction defaults to Accepted (ACSC): zero clicks for the happy path
- [x] Rejected needs a reason code (AC01, AM04, RC01, MS03 and more); Pending is PDNG
- [x] The reply has a new `MsgId`/`CreDtTm`, references the original `MsgId`, and one `TxInfAndSts` per transaction with its
      `OrgnlEndToEndId`, `TxSts` and, for rejections, `StsRsnInf/Rsn/Cd`
- [x] The reply is validated against its own XSD before download; an invalid reply is never offered
- [x] Covered for both pain.001 versions with multi-transaction, multi-`PmtInf` samples and a mix of accepted, rejected and pending

## Roadmap

| Version | Content |
|---|---|
| v0.1 | pain.001 XSD validation (.03, .09), CLI + Web UI |
| v0.2 | Message generation from a template (key fields, generated IDs) |
| v0.3 | Batch test-suite generation (valid + invalid files, zip with manifest) |
| v0.4 | pain.001 business rules (`CtrlSum`, `NbOfTxs`, IBAN checksum, execution date) + **Test** mode (code editor, edit-and-revalidate) |
| v0.5 | **Simulate**: pain.002 status report for a pain.001, status per transaction (.03 -> .03, .09 -> .10) |
| v0.6 | pacs.008 support |
| v0.7 | Plain-language error explanations |
| v0.8 | Scheme rules: SWIFT CBPR+, Fedwire |
| v0.9 | Multi-transaction editing, more error types (incl. business-rule errors), more business rules (BIC, per-country IBAN length) |

## Sources

Only public schemas are used. iso20022.org blocks scripted downloads (404/403, tried for every schema), so all four XSDs come from
public mirrors of the ISO 20022 schemas. Compare with the official downloads at iso20022.org before relying on them.

| Schema | Generator build (in file header) | Mirror |
|---|---|---|
| pain.001.001.09 | Standards Editor R1.6.15, 2019 | <https://github.com/fortesp/xsd2xml> (`tests/resources/`) |
| pain.001.001.03 | SWIFTStandards Workstation R6.1.0.2, 2009 | <https://github.com/jasperkrijgsman/dutch-sepa-iso20022> (`src/main/resources/`) |
| pain.002.001.03 | SWIFTStandards Workstation R6.1.0.2, 2009 | <https://github.com/EggBaconAndSpam/iso20022-schemas> (`pain.002.001.03.xsd`; byte-for-size identical to the copy in `sebastienrousseau/pain001`) |
| pain.002.001.10 | Standards Editor R1.6.15, 2019 | <https://github.com/EggBaconAndSpam/iso20022-schemas> (`pain.002.001.10.xsd`) |

# Synthetic test fixtures — NOT real people

Every file in this directory is synthetic test data generated for MEYAR's
test suite. No real candidate, real CV, or real personal information is
present. See `docs/SECURITY_PRIVACY.md` — real CVs must never be committed.

- `valid_cv.pdf`, `valid_cv.docx` — minimal valid documents with
  extractable synthetic text.
- `prompt_injection_cv.pdf`, `prompt_injection_cv.docx` — contain
  instruction-injection text ("ignore all previous instructions...") to
  verify it is always treated as inert document data, never as an
  instruction.
- `malformed.pdf` — valid PDF signature, invalid internal structure
  (parse failure fixture).
- `malformed.docx` — valid zip, missing the OOXML `word/document.xml`
  entry (rejected at upload validation, not parse time).
- `unsupported.txt`, `unsupported.png` — unsupported file types.
- `wrong_extension.pdf` — real DOCX bytes saved with a `.pdf` extension
  (extension/signature mismatch fixture).

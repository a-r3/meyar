## Scope

<!-- What changed, in 1-3 sentences. Link the slice/decision it belongs to. -->

## Verification

- [ ] Ruff passed (`uv run ruff check .`)
- [ ] mypy project gate passed (`uv run mypy src`)
- [ ] pytest passed (`uv run pytest -q`)
- [ ] Relevant live/smoke test performed where appropriate

## Security / Privacy

- [ ] No secret/API key committed
- [ ] No real CV/PII committed
- [ ] No candidate data sent to external AI/service
- [ ] Tenant/access boundaries reviewed if affected

## Architecture / Documentation

- [ ] `docs/STATUS.md` updated when material
- [ ] `docs/DECISIONS.md` updated for material architecture/policy changes
- [ ] API/schema/migration compatibility reviewed where relevant

## Deferred / Known gaps

<!-- Short list, or "none". -->

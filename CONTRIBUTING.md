# Contributing

Phantom is opinionated. The project exists to provide an anonymous AI inference proxy with verifiable privacy properties. Contributions are welcome but must preserve those properties.

## Scope

Phantom does:
- Forward inference requests to TEE-attested upstream gateways
- Accept anonymous crypto payment
- Issue OpenAI-compatible bearer keys

Phantom does NOT:
- Log IPs, prompt content, completion content, or request bodies
- Maintain user accounts, email lists, or identity records
- Add third-party JavaScript, fonts, analytics, or CDN dependencies
- Persist secrets (DB passphrase, API keys, wallet passwords) to disk

PRs that introduce telemetry, tracking, account systems, third-party JS, Postgres/Redis dependencies, or Docker as the deployment story will be closed. The architecture is intentional. Read `HANDOFF.md` before opening one.

## Coding style

- Python 3.12. Type hints encouraged, not required.
- `async/await` for IO. Single SQLCipher connection serialised via `asyncio.Lock`.
- Money is integer micro-USD. Never `float`. See `CLAUDE.md`.
- XMR amounts are `Decimal` + piconero strings. Never float.
- Timestamps are `datetime.now(timezone.utc).isoformat()`. Never `utcnow()`.
- HTTPException reasons are lowercase. Generic upstream failures map to `503 "upstream unavailable"`.
- Body whitelists on every endpoint that proxies to Redpill — do not relax them.

## Tests

Required to pass before merging:
```bash
python -m pytest tests/ -v
```

New endpoints must add a test in `tests/`. New cost-math changes must add coverage for the boundary cases.

## PRs

Keep them small. One thing per PR. Reference the privacy invariant your change touches, if any.

Commit messages: lowercase, imperative, no period.
```
config: tighten max_tokens clamp
```

## Security disclosure

Encrypted only. Public PGP key in `frontend/pgp.txt`. Adopters running their own phantom instance MUST replace that key with their own — the published default is a placeholder.

If you find a privacy regression (something that could log a prompt, leak a key, deanonymise a customer, or violate the "no third-party assets" rule), report it via the operator's PGP-encrypted contact rather than opening a public issue.

## License

By contributing, you agree your contribution is licensed under AGPL-3.0 (see `LICENSE`).

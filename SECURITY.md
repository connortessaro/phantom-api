# Security Policy

## Reporting a vulnerability

Encrypted only. Public PGP key in `frontend/pgp.txt`.

**Important for adopters:** the PGP key shipped in this repo is a `<YOUR-PGP-FINGERPRINT>` placeholder. Generate your own key before running an instance, replace `frontend/pgp.txt`, and update the fingerprint references in `frontend/terms.html` + `frontend/docs.html`.

## What's in scope

Anything that could:

- Log or persist a customer's IP address, prompt content, completion content, or API key plaintext
- Allow an attacker to claim an API key issued to another customer
- Bypass the per-key credit decrement (free inference)
- Break the SQLCipher-at-rest invariant
- Leak the operator's wallet seed or API keys via process listing, log files, or env exfiltration
- Forge an attestation report or signature claim

## What's not in scope

- The operator hosting your instance can be subpoenaed. Phantom's design minimises what they CAN disclose; it does not make the operator immune.
- Physical seizure of an unlocked VPS exposes the live SQLCipher decrypted memory image. Mitigation is operational (do not unlock unless serving).
- Supply-chain attacks on `requirements.lock`. Phantom installs with `--require-hashes`, but cannot detect malicious crates introduced before the lockfile was generated.
- Vendor-side logging for `PROXY` tier models. Customers pay anonymously, but OpenAI/Anthropic/Google still read the content. Phantom only hides identity, not content. This is documented in `frontend/terms.html#privacy`.

## Threat model

`HANDOFF.md` carries the full design rationale and threat model. Skim that before reporting; many "vulnerabilities" are documented trade-offs.

## Disclosure timeline

Best-effort acknowledgment within 5 business days. Coordinated disclosure window: 90 days from first contact unless an active exploit warrants faster.

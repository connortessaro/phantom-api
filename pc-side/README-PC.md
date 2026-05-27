# Operator PC setup

The wallet lives here, not on the VPS. VPS reaches it via Tor hidden service.

## One-time

- [ ] Install Tor (`brew install tor` macOS / `sudo apt install tor` Linux)
- [ ] Install Monero CLI (verify GPG signature from getmonero.org)
- [ ] Create wallet with `monero-wallet-cli`. Back up view + spend keys offline.
- [ ] Append `torrc.example` contents to system `torrc`. Restart Tor.
- [ ] Capture onion hostname: `sudo cat /var/lib/tor/phantom-wallet/hostname`
- [ ] Set `WALLET_ONION=<hostname>` in VPS `.env`
- [ ] Write `~/.phantom-wallet/wallet-password` (wallet open passphrase, mode 0600)
- [ ] Run `./start-wallet.sh`. Generates `~/.phantom-wallet/rpc-password` first run.
- [ ] Set `WALLET_RPC_PASSWORD=<contents-of-rpc-password>` in VPS `.env`

## Operational

- [ ] PC must be on whenever payments need confirmation
- [ ] Cron `sweep.py` daily — keeps hot float < ~$500
- [ ] Cold wallet seed on paper, in a safe, NEVER on this PC
- [ ] Block inbound 18083 from anywhere via host firewall — only Tor reaches it locally

## Failure modes

- PC offline → payments stall → users see `pending`/`confirming` longer. Acceptable for short outages. Set a status note on the site.
- Wallet crashes mid-poll → `start-wallet.sh` as a `launchd`/systemd-user unit with restart=on-failure.

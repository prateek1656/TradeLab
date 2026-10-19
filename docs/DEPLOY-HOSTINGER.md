# Putting TradeLab online on a Hostinger VPS

This replaces **Parts 1, 2 and the start of Part 4** of the main guide ([DEPLOY.md](DEPLOY.md)) for a Hostinger VPS. Everything else — the domain, Docker, downloading TradeLab, settings, starting it, Kotak, backups and alerts — is the same, so you follow the main guide for those, with the few differences listed below.

Prices and plan details change; check them on Hostinger's VPS page before buying.

---

## Which plan

| Plan | CPU / memory / disk | Good for |
|---|---|---|
| **KVM 1** | 1 vCPU · 4 GB · 50 GB NVMe | **The pilot and full use**: one user with up to 10 accounts on one IP (Kotak's family limit). 4 GB is double what TradeLab needs with 15 accounts |
| KVM 2 | 2 vCPU · 8 GB · 100 GB NVMe | Only if you later run more on the same server; not needed for TradeLab |

**Choose KVM 1.** Unlike a 1 GB server, it doesn't depend on swap at 09:15, so there's no need to upgrade before adding accounts. The single CPU only makes builds and updates slower (10–15 minutes); placing orders mostly waits on Kotak's replies, not on the CPU.

**Billing period:** the low monthly price usually needs a long term (12–24 months) and renews higher. A short first term costs more per month but lets you finish the pilot first.

**Remember Kotak's static IP rule:** one IP can be shared by at most 10 accounts, and only family linked in Kotak's Trade API family management (clients' accounts can't share it). Accounts that can't share it need their own server with its own IP.

---

## Part 1 (Hostinger) — Create the server

1. On Hostinger, buy **VPS → KVM 1**.
2. **Location:** choose **India** (Mumbai) — close to NSE and Kotak.
3. **Operating system:** **Ubuntu 24.04** (plain; you install Docker yourself in Part 4.3). If you pick Hostinger's "Ubuntu 24.04 with Docker" template instead, skip Part 4.3.
4. **Root password:** set a long, random one and keep it in your password manager. (Optionally also add your SSH key if you know how.)
5. Wait until the VPS shows **Running** in **hPanel → VPS**.

## Part 2 (Hostinger) — The IP address and firewall

**Your static IP:** hPanel → VPS → **Overview** shows the server's **IPv4 address**. This is `YOUR.SERVER.IP` everywhere in the main guide, and the address you register with Kotak. It belongs to this VPS for as long as you keep it — upgrading the plan keeps it too — so **never delete and recreate the VPS**: a new VPS gets a new IP, and Kotak allows an IP change only once every 7 days.

**Firewall:** hPanel → VPS → **Security → Firewall** → create a firewall configuration with these **Accept** rules, then activate it for this VPS:

| Protocol | Port | Source |
|---|---|---|
| TCP | 22 (SSH) | Any |
| TCP | 80 (HTTP) | Any |
| TCP | 443 (HTTPS) | Any |

Everything else stays closed. (The database is never published to the internet anyway.)

## Part 3 — Point your domain at the server

As in the main guide: an **A** record named `trade`, value `YOUR.SERVER.IP`. If your domain is at Hostinger: hPanel → **Domains → ssenterprises.tech → DNS / Nameservers → DNS records** → add the A record. Delete any other A or AAAA record named `trade`.

## Part 4 (Hostinger) — Connect to the server

Either:

- hPanel → VPS → **Browser terminal** (opens a terminal in your browser), or
- from Windows **PowerShell** or **Terminal**: `ssh root@YOUR.SERVER.IP` and type the root password (nothing shows while you type — that's normal).

You are logged in as **root**, so in the main guide:

- **4.1** (updates, time zone): the same.
- **4.2** (swap): optional on KVM 1 (4 GB); harmless to add.
- **4.3** (Docker): the same, but skip the last line (`sudo usermod -aG docker $USER`) and skip reconnecting — root can already use Docker. Skip 4.3 entirely if you chose the "Ubuntu with Docker" template.
- **4.4** (log limit): the same.

Then continue with **Part 5** of the main guide (download TradeLab) and everything after it exactly as written. Commands with `sudo` work as root too.

## Backups on Hostinger

Do Part 11 of the main guide (the nightly backup script). For copies off the server, hPanel → VPS → **Backups & monitoring** shows Hostinger's automatic backups and lets you take a **snapshot** — take one after setup and before every update.

## Moving to another Hostinger plan

Upgrade in hPanel (VPS → **Upgrade**): the same VPS gets more resources and keeps its IP, so Kotak needs no change. Do it outside market hours; the VPS restarts once.

# Putting TradeLab online with your own domain

This guide takes you from nothing to TradeLab running at `https://trade.ssenterprises.tech`, step by step. It uses **AWS Lightsail in Mumbai** because it is simple, close to NSE, and gives a free static IP while it's in use. Any Ubuntu server works the same way from Part 4 on.

This copy is filled in for **ssenterprises.tech**: TradeLab will live at `https://trade.ssenterprises.tech` (a subdomain, so the main domain stays free for a company website). Everywhere below, replace `YOUR.SERVER.IP` with your server's static IP (you get it in Part 2).

Lines in grey boxes are commands: copy them, paste them into the server window, and press Enter. Paste one box at a time.

**Do this on a weekend or an evening**, not during market hours. It takes about an hour, plus waiting for the domain to start pointing at the server.

---

## Before you start

You need:

- the **domain** you bought, and the login of the website you bought it from (GoDaddy, Hostinger, Namecheap, BigRock, Cloudflare…);
- an **AWS account** (aws.amazon.com, needs a card);
- your **GitHub** login (the code is in the private repository `Adarsh-Saxena/TradeLab`);
- for each Kotak account: the **consumer key, mobile number, UCC, MPIN and TOTP secret** (see the README, *Prepare each Kotak Neo account*).

> **Check Kotak's static IP rule first. It decides how many servers you need.**
> Since 1 April 2026, Kotak accepts Trade API orders only from a **static IP registered in that account**, and the login must come from the same IP. Several accounts can use **one** IP only if they are linked as **family** in Kotak's *Trade API family management*, and Kotak's page says **up to 10 members**. Under SEBI's rules, family means yourself, your spouse, dependent children and dependent parents. **A client's account is not family**, so it cannot share your IP.
>
> TradeLab sends every account's orders from the one server it runs on, so **every account in one TradeLab must be allowed to use that server's IP**. Accounts that can't share it need their own server (a second TradeLab at, for example, `trade2.ssenterprises.tech`, set up with this same guide). Ask Kotak to confirm which of your accounts can share one IP **before** you register anything, because a registered IP can be changed only **once every 7 days**.

---

## Part 1 — Create the server

1. Go to **lightsail.aws.amazon.com** and sign in.
2. Click **Create instance**.
3. **Instance location:** click *Change AWS Region and Availability Zone* and choose **Mumbai (ap-south-1)**.
4. **Platform:** Linux/Unix. **Blueprint:** *OS Only* → **Ubuntu 24.04 LTS**.
5. **Networking type:** choose **Dual-stack** (it has an IPv4 address). Don't choose an *IPv6-only* plan: you need an IPv4 address to register with Kotak, and GitHub can't be reached over IPv6 only.
6. **Plan:** for testing, the plan with **1 GB memory** is enough: TradeLab uses about 300 MB when running. Building it needs more, which the 2 GB swap in Part 4.2 covers (**don't skip 4.2 on 1 GB**); the first build then takes 10–20 minutes. Before trading with all accounts, move to 2 GB (see *Moving to a bigger or new server*: same IP, no change at Kotak).
7. **Name:** `tradelab`. Click **Create instance**.
8. Wait until it says **Running** (about a minute).

## Part 2 — Give it a static IP and open the HTTPS port

**Static IP** (this is the address you'll register with Kotak, so it must never change):

1. Lightsail home → **Networking** tab → **Create static IP**.
2. Region: Mumbai. **Attach to an instance:** `tradelab`. Name: `tradelab-ip`. Click **Create**.
3. Write down the IP it shows. This is `YOUR.SERVER.IP`.

(The static IP is free while it's attached to an instance. Never delete it: if you move to a new server later, you detach it and attach it to the new one, so Kotak never needs a new IP.)

**Firewall:**

1. Open the `tradelab` instance → **Networking** tab → **IPv4 Firewall**.
2. You should see **SSH (22)** and **HTTP (80)**. Click **Add rule** → Application **HTTPS** (port 443) → **Create**.
3. Leave everything else closed. The database is never reachable from the internet.

## Part 3 — Point your domain at the server

Do this now, because it can take from a few minutes to a few hours to start working.

1. Sign in where you bought the domain and open its **DNS** settings (often called *Manage DNS*, *DNS Zone* or *DNS records*).
2. **Add a record:**
   - **Type:** `A`
   - **Name / Host:** `trade` (for `trade.ssenterprises.tech`; use `@` only if you want the bare domain)
   - **Value / Points to:** `YOUR.SERVER.IP`
   - **TTL:** the lowest offered (for example 600 seconds or ½ hour)
3. If there is already an **A** or **AAAA** record with the same name, delete it. An old **AAAA** (IPv6) record makes the HTTPS certificate fail.
4. **If your DNS is at Cloudflare:** set the record to **DNS only** (grey cloud), not *Proxied*.

To check: open **dnschecker.org**, enter `trade.ssenterprises.tech`, type **A**. When most places show `YOUR.SERVER.IP`, it's working.

## Part 4 — Prepare the server

**Open the server window:** in Lightsail, on the `tradelab` instance, click **Connect using SSH**. A black terminal window opens in your browser. Paste with right-click → Paste (or Ctrl+Shift+V).

**4.1 Update Ubuntu, and set the clock to Indian time** (only for readable dates in logs and backups; TradeLab always works in IST anyway):

```bash
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone Asia/Kolkata
timedatectl
```

The last command must show **System clock synchronized: yes**. Orders are timed to the second, so the clock must be right (Ubuntu keeps it in sync by itself).

If the upgrade says a restart is needed, run `sudo reboot`, wait a minute, and click **Connect using SSH** again.

**4.2 Add 2 GB of swap** (required on the 1 GB plan), so building the app never runs out of memory:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**4.3 Install Docker:**

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
```

**4.4 Stop Docker's logs from filling the disk** over the months:

```bash
sudo mkdir -p /etc/docker
echo '{ "log-driver": "local" }' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

Now **close the SSH window and click Connect using SSH again**, so you can use Docker without `sudo`. Check:

```bash
docker run --rm hello-world
```

It should print *Hello from Docker!*.

## Part 5 — Download TradeLab onto the server

The repository is private, so the server needs its own read-only key to GitHub.

**5.1 Make the key on the server:**

```bash
ssh-keygen -t ed25519 -C "tradelab-server" -f ~/.ssh/tradelab_deploy -N ""
printf 'Host github.com\n  IdentityFile ~/.ssh/tradelab_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
chmod 600 ~/.ssh/config
cat ~/.ssh/tradelab_deploy.pub
```

Copy the whole line the last command prints (it starts with `ssh-ed25519`).

**5.2 Give it to GitHub:** on github.com open **Adarsh-Saxena/TradeLab** → **Settings** → **Deploy keys** → **Add deploy key**. Title: `tradelab-server`. Paste the line into **Key**. Leave **Allow write access** unticked. Click **Add key**.

**5.3 Download the code:**

```bash
cd ~
git clone -b claude/intraday-trading-app-ci0hhp git@github.com:Adarsh-Saxena/TradeLab.git tradelab
```

The first time, it asks *Are you sure you want to continue connecting?* Type `yes` and press Enter. (To be extra careful, first check that the fingerprint it shows is one of those on GitHub's page *GitHub's SSH key fingerprints*.)

## Part 6 — Settings

**6.1 Make a secret key and write it down safely.** It encrypts the Kotak details stored on the server. You need it to restore a backup on a new server.

```bash
openssl rand -hex 32
```

Copy the long string it prints into your **password manager** (or write it on paper kept somewhere safe). Never share it.

**6.2 Create the settings file:**

```bash
cd ~/tradelab
cp .env.example .env
nano .env
```

A simple editor opens. Change these two lines (use the arrow keys to move):

```
DOMAIN=trade.ssenterprises.tech
TRADELAB_SECRET_KEY=the-long-string-from-6.1
```

Leave the rest as it is. Save with **Ctrl+O**, then **Enter**, and exit with **Ctrl+X**. Then make the file private:

```bash
chmod 600 .env
```

## Part 7 — Start TradeLab

Make sure Part 3 is working first (dnschecker shows your IP). Then:

```bash
cd ~/tradelab
docker compose up -d --build
```

The first time takes about 5–10 minutes: it builds the app and downloads MongoDB and Caddy. Then check:

```bash
docker compose ps
```

You should see three services: **mongo** (*healthy*), **tradelab** (*Up*) and **caddy** (*Up*). Now check the HTTPS certificate:

```bash
docker compose logs caddy | grep -i "certificate obtained"
```

You should see **certificate obtained successfully**. If nothing shows yet, wait a minute and try again. If it still doesn't work, see *Troubleshooting* below.

Last check:

```bash
curl -s https://trade.ssenterprises.tech/api/health
```

It must print `{"ok":true}`.

TradeLab now starts by itself whenever the server restarts. You can close the SSH window: it keeps running.

## Part 8 — Create your account straight away

1. Open `https://trade.ssenterprises.tech` on your phone or laptop.
2. Choose **Create an account**, then enter your name, email and a strong password.

Do this immediately: the **first account created is the owner**, the only one who can change the holiday calendar. Anyone who knows the address can sign up for their own empty space, so keep the address private.

## Part 9 — Register the server's IP with Kotak

For **each** Kotak account (keeping in mind the family limit in *Before you start*):

1. Kotak Neo app (or website) → **More** → **Trade API**.
2. In the **static IP** section, enter `YOUR.SERVER.IP` as the **primary** IP and save.
3. For accounts that share the IP as family: add them under **Trade API family management**. The family members you already linked for the app's login don't carry over.

Register the IP **before** adding the accounts to TradeLab, because Kotak accepts orders only from a session logged in from the registered IP. Double-check every digit: a wrong IP can be corrected only after 7 days.

## Part 10 — Set up TradeLab

1. **Accounts → Add account** for each Kotak account. If it says **Connected**, that account is done. If it says *this server's IP address isn't whitelisted for this account*, Part 9 isn't active yet for that account. Wait, then try again.
2. **Settings → Market holidays:** check that NSE's calendar loaded. If it shows a download error, add the year's holidays by hand.
3. **Install the app:**
   - Android: open the address in Chrome → **⋮** → **Install app**;
   - Windows: open it in Edge or Chrome → click the **Install** icon in the address bar.
4. Add your stocks and contracts (see the manual).

## Part 11 — Backups (important)

The database is TradeLab's memory of what it bought and sold first. Losing it means closing positions by hand in every account.

**11.1 A daily backup on the server.** Create the backup script:

```bash
cat > ~/tradelab-backup.sh <<'EOF'
#!/bin/sh
# TradeLab backup: the database and the data volume, kept for 30 days.
set -e
cd "$HOME/tradelab"
mkdir -p "$HOME/backups"
day=$(date +%F)
docker compose exec -T mongo mongodump --archive --gzip --oplog > "$HOME/backups/tradelab-db-$day.archive.gz"
docker run --rm -v tradelab_tradelab-data:/data:ro -v "$HOME/backups":/backup alpine tar czf "/backup/tradelab-data-$day.tgz" -C /data .
find "$HOME/backups" -name 'tradelab-*' -mtime +30 -delete
echo "$(date) backup ok"
EOF
chmod +x ~/tradelab-backup.sh
~/tradelab-backup.sh
ls -lh ~/backups
```

The script must print **backup ok**, and `ls` must list two files. Then make it run every night at 01:00:

```bash
(crontab -l 2>/dev/null; echo '0 1 * * * $HOME/tradelab-backup.sh >> $HOME/backups/backup.log 2>&1') | crontab -
```

**11.2 Copies off the server.** In Lightsail, open the `tradelab` instance → **Snapshots** → turn on **Automatic snapshots**. Each snapshot includes the backup files above. Snapshots cost a little per month.

**11.3 Keep these somewhere safe, off the server:** the secret key from 6.1 and the `.env` file contents. Without the key, the Kotak logins inside a backup can't be decrypted, and every account would have to be added again.

## Part 12 — Get an alert on your phone if the server goes down

TradeLab itself has no phone alerts. A free uptime checker fills that gap:

1. Sign up at **uptimerobot.com** (free).
2. **Add New Monitor** → type **HTTP(s)** → URL `https://trade.ssenterprises.tech/api/health` → every 5 minutes.
3. Choose how to be alerted (email, or their phone app).

If the server stops, you'll know before 09:15.

## Part 13 — The first live day

Follow **Start small** in the manual: one account, **1 share / 1 lot** in each section (including one sell-first trade), and check every order in the Kotak Neo app. Watch the first 14:30 square-off as it happens. Add the other accounts only after that has worked.

---

## Looking after it

All commands run in the SSH window, from the `~/tradelab` folder (`cd ~/tradelab` first).

| To… | Run |
|---|---|
| See if everything is running | `docker compose ps` |
| See TradeLab's recent messages | `docker compose logs --tail 100 tradelab` |
| Restart TradeLab | `docker compose restart tradelab` |
| See free disk space | `df -h /` |

**Updating to a new version of TradeLab:**

```bash
cd ~/tradelab
~/tradelab-backup.sh
git pull
docker compose up -d --build
docker compose ps
```

Update on a weekend or in the evening, when you aren't about to tap Buy, Sell or Exit. **Never between 08:55 and 15:30 on a trading day**: that's when the timed Intraday buy and square-off happen. A restart lets orders already being sent finish (up to 40 seconds) and takes about a minute in all.

**Ubuntu updates:** security updates install by themselves. When the SSH window says *System restart required*, run `sudo reboot` on a weekend. TradeLab comes back on its own.

**Moving to a bigger or new server** (keeping the same IP, so Kotak and your domain need no change):

1. On the old server, **stop TradeLab** first, so two copies never trade the same accounts:
   ```bash
   cd ~/tradelab && docker compose stop tradelab
   ```
2. In Lightsail, open the old instance → **Snapshots** → **Create snapshot**, and wait until it's ready.
3. On the snapshot, choose **Create new instance** (you can pick a bigger plan here). Keep **Mumbai**.
4. Go to **Networking**, detach `tradelab-ip` from the old instance and attach it to the new one. Open port **443** on the new instance's firewall (Part 2).
5. Connect to the new instance and run `cd ~/tradelab && docker compose up -d` then `docker compose ps`. Check the app opens.
6. Delete the old instance only after a day of normal trading on the new one. **Never start TradeLab on the old one again.**

**If the server is lost or broken:** create a new instance from the latest **automatic snapshot** (step 3 above), then do steps 4 and 5. To restore just the database from a nightly backup instead, see *Back up two things* in the README.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| The address doesn't open, or the browser warns about the certificate | Check dnschecker.org shows `YOUR.SERVER.IP` (Part 3), that there is no AAAA record for the name, and that the Lightsail firewall has ports 80 **and** 443 open (Part 2). Then `docker compose restart caddy` and look at `docker compose logs caddy`. After many failed tries, the certificate service makes you wait about an hour. |
| `docker compose ps` shows **tradelab** as *unhealthy*, or UptimeRobot alerts | Open `https://trade.ssenterprises.tech/api/health`: it lists the problem (database or scheduler). TradeLab restarts itself if it stays stuck for about 90 seconds; if the database is the problem, `docker compose restart mongo`. Then check `docker compose logs --tail 100 tradelab`. |
| **502 Bad Gateway** | TradeLab isn't running. Look at `docker compose logs --tail 100 tradelab`. |
| **mongo** never becomes *healthy*, or its log says *Illegal instruction* | The server's processor is too old for MongoDB 8 (it needs AVX). Lightsail servers are fine. On another provider, pick a newer server type. |
| The build stops with *Killed* or exit code 137 | Out of memory: check that the swap from 4.2 is on (`free -h` shows *Swap: 2.0Gi*), then run the start command again. |
| Adding an account says the server's **IP address isn't whitelisted** | Register `YOUR.SERVER.IP` in that Kotak account (Part 9), wait for Kotak to activate it, and try again. |
| `git pull` says *Permission denied (publickey)* | The deploy key was removed from GitHub. Repeat 5.2 with the line from `cat ~/.ssh/tradelab_deploy.pub`. |
| The disk is getting full | While everything is running, `docker system prune -f` removes old build leftovers (never your data). Old backups are deleted after 30 days. |

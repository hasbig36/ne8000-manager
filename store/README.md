# NE8000 Manager licence store

Sell NE8000 Manager to other operators: a public **sales site** (features, pricing, checkout) and a
**billing site** (customer accounts, invoices, renewals, admin), in one small Flask app.

## How a sale works
1. The buyer enters how many routers they have. The price is **$100 for the first device and $30 for
   each extra device, per month** (settings `STORE_FIRST_DEVICE_USD`, `STORE_EXTRA_DEVICE_USD`, `STORE_PERIOD`).
2. They see the recommended server size, and choose **their own server** or **hosted by us**
   (DigitalOcean monthly price + `STORE_HOSTING_MARKUP`%, 30% by default).
3. They create a billing account and pay the invoice:
   - **M-Pesa**: an STK push prompt on their phone. Daraja calls back, and the store asks Daraja again
     (STK query) before marking the invoice paid, so a fake callback cannot activate a licence.
   - **Bank transfer**: they see your bank details with the invoice number as reference; you mark it
     paid on the admin page when the money arrives.
4. When the invoice is paid the licence becomes active and:
   - **Own server**: they get an email with their licence key and a one-line installer
     (`curl -fsSL -H 'X-Licence-Key: …' https://billing…/api/install.sh | sudo sh`).
   - **Hosted**: the store creates a DigitalOcean droplet whose first boot downloads and installs the
     app with HTTPS at `<ip>.sslip.io`, then reports back; the store emails the address, `admin` and a
     generated password (the password is deleted from the store after sending).
5. Seven days before the month ends a renewal invoice is emailed. After the paid period plus
   `STORE_GRACE_DAYS` (7) the licence expires and the app stops connecting to routers until renewed.
   Customers can add devices from their account at any time ($30 each).

## Licences in the app
The store signs licences with an Ed25519 key it creates on first start (`data/licence_signing.key`,
**back this file up**: without it, existing installs cannot verify new licences). Installs get
`NE8000_LICENCE_KEY`, `NE8000_LICENCE_SERVER` and `NE8000_LICENCE_PUBKEY` in their `.env`; the app
(`licence.py`) fetches the signed licence every 12 hours, caches it, and limits saved routers to the
devices paid for. Your own install has no `NE8000_LICENCE_KEY`, so it has no limit.

## Run it
On a small Ubuntu server with Docker, DNS for both addresses pointing at it, ports 80/443 open:

```sh
git clone … ne8000-manager && cd ne8000-manager
cp store/.env.example store/.env && nano store/.env      # fill in, see below
docker compose -f store/docker-compose.yml up -d --build
```

Caddy gets HTTPS certificates for both addresses. Admin page: `https://<billing address>/admin`.

Local try-out without any accounts: `cd store && pip install -r requirements.txt && STORE_DEV=1 python app.py`
then open http://127.0.0.1:8090 (M-Pesa and hosting show as not available; emails go to `data/outbox`).

Tests: `python -m pytest store/tests -q` (M-Pesa and DigitalOcean are faked).

## Settings to fill in (store/.env)
| What | Settings | Where to get it |
|---|---|---|
| Addresses | `STORE_SALES_HOST`, `STORE_BILLING_HOST`, `STORE_PUBLIC_URL` | Your domain's DNS |
| Admin | `STORE_ADMIN_PASSWORD`, `STORE_ADMIN_EMAIL` | Choose |
| M-Pesa | `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_TILL`, `MPESA_CALLBACK_SECRET` | developer.safaricom.co.ke: create an app with Lipa na M-Pesa Online. Test with `MPESA_ENV=sandbox`, then go live and set `production` |
| Bank | `BANK_NAME`, `BANK_ACCOUNT_NAME`, `BANK_ACCOUNT_NUMBER`, … | Your bank |
| Hosting | `DO_TOKEN`, `DO_REGION`, `DO_SSH_KEYS` | DigitalOcean › API › Generate token (read + write) |
| Email | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Your mail provider (for Gmail, an app password) |
| Exchange rate | `STORE_KES_PER_USD` | Update now and then |

Until M-Pesa or DigitalOcean are set up, the site offers bank transfer and own-server installs only.

## Server sizes
| Devices | DigitalOcean size | Droplet price (fallback) | Hosted price (+30%) |
|---|---|---|---|
| 1–2 | s-1vcpu-2gb | $12 | $15.60 |
| 3–5 | s-2vcpu-4gb | $24 | $31.20 |
| 6–15 | s-4vcpu-8gb | $48 | $62.40 |
| 16–40 | s-8vcpu-16gb | $96 | $124.80 |
| 41–200 | g-8vcpu-32gb | $252 | $327.60 |

With `DO_TOKEN` set, the live DigitalOcean prices are used instead. Edit `SIZES` in `pricing.py` to change the tiers.
When a hosted customer adds enough devices to need a bigger size, you get an email to resize the droplet.

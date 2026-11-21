# Tharai Basket

A premium green-and-white hyperlocal grocery storefront and operations dashboard for Tharamangalam, Salem, Tamil Nadu.

## Included in this starter release

- Customer storefront, product catalogue and product detail pages
- Customer registration/login, session cart, checkout and order history
- Admin dashboard with product creation, category management, order workflow, supplier contacts and supplier price records
- Purchase-list generation from customer orders
- Delivery partner accounts, assignment and delivery status updates
- Delivery earning records
- Configurable free-delivery threshold, delivery fee and minimum order value
- SQLite for local use; PostgreSQL URL support for hosted deployment
- CSRF protection, password hashing, role checks and server-side cart price calculations

## Important launch notes

This is a functional MVP foundation, not yet a fully audited production commerce system. Before taking live payments or public orders:

1. Replace sample product prices and verify every supplier price, quality, unit and tax treatment.
2. Configure a strong `SECRET_KEY` and persistent PostgreSQL database.
3. Configure and test a real payment gateway. UPI in this release is only recorded as awaiting confirmation; no money is collected automatically.
4. Configure WhatsApp/SMS notifications if needed.
5. Add production image storage (local uploads are ephemeral on many hosts).
6. Review FSSAI, GST, local trade, consumer-protection, privacy and other applicable compliance requirements for the exact operating model.
7. Test order cancellation/refunds, stock reconciliation, supplier purchase costing, delivery partner payments and backup/restore before launch.

## Local setup (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY="replace-with-a-long-random-secret"
flask --app wsgi:app init-db
flask --app wsgi:app seed-demo
flask --app wsgi:app create-admin
python run.py
```

Open `http://127.0.0.1:5000`.

On macOS/Linux, activate with `source .venv/bin/activate` and set environment variables using `export`.

## Admin account

Create an administrator interactively:

```bash
flask --app wsgi:app create-admin
```

Do not use a shared or default admin password. Keep credentials private.

## Demo data

```bash
flask --app wsgi:app seed-demo
```

Demo prices are examples only. Replace them with verified wholesale costs and approved selling prices before enabling orders.

## Render deployment

A `render.yaml` blueprint is included. For deployment:

1. Push this project to a private or public GitHub repository.
2. In Render, create a Blueprint from that repository.
3. Confirm the PostgreSQL database and web service resources.
4. Set a strong `SECRET_KEY` in the Render environment.
5. Deploy, then use Render Shell to run `flask --app wsgi:app create-admin` and `flask --app wsgi:app seed-demo` as appropriate.
6. Verify `/health`, customer signup, admin login, product management and test checkout.

Persistent uploads should be moved to an object-storage service before production because the default local upload folder may not persist across redeploys.

## GitHub upload

Create a repository named `tharai-basket`, then upload the contents of this project folder (not the enclosing parent folder). Alternatively:

```bash
git init
git add .
git commit -m "Initial Tharai Basket MVP"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/tharai-basket.git
git push -u origin main
```

## Configuration

See `.env.example`. Never commit `.env`, credentials, payment keys or customer data.

## Operations and reliability

- Admin quality reports are available at **Admin → Quality reports**. Admin status changes and response notes generate an in-app customer notification and appear on the customer's order page.
- The analytics overview includes delivered revenue/profit, category performance, peak order hours, repeat-customer metrics and open quality reports.
- Customer storefront recommendations and one-click Buy Again use the customer's delivered-order history and current product stock.
- Run `flask --app wsgi:app backup-db` for a consistent local SQLite backup. PostgreSQL backups should be configured as managed database backups/snapshots in Render; the SQLite command intentionally does not attempt a PostgreSQL dump.
- Session cookies are marked Secure, HttpOnly and SameSite=Lax. Standard browser security response headers are added by the app.
- Local uploaded photos can be ephemeral on Render. Configure persistent object storage or a persistent disk before relying on complaint photos for long-term evidence.

## Current limitations to address before launch

- No live Razorpay/UPI payment integration is activated.
- No live GPS tracking or automated route optimisation.
- Supplier purchases can be generated as lists, but supplier assignment, actual cost entry and stock reconciliation need a full purchasing workflow before production.
- Product edit UI, refund workflow, cancellation rules, coupon redemption and customer address book are not yet complete.
- Admin-created delivery partner passwords should be temporary and changed securely; add password-reset flow before launch.
- Upload validation checks extension; production deployment should also inspect file content and use persistent object storage.
- This MVP does not include automated SMS/email/WhatsApp notifications.

## License

Private project. Add an appropriate license before public distribution.

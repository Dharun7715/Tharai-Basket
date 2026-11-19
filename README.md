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

## Render deployment

A `render.yaml` blueprint is included. Create a Blueprint from this repository, confirm PostgreSQL and web service resources, set a strong `SECRET_KEY`, deploy, then create an admin account and seed demo data through Render Shell.

## GitHub upload

The full project folder must be pushed to the repository root. Never commit `.env`, credentials, payment keys or customer data.

## Current limitations

- No live Razorpay/UPI payment integration is activated.
- No live GPS tracking or automated route optimisation.
- Supplier purchasing, actual cost entry and stock reconciliation need further production work.
- Refunds, cancellation rules, coupon redemption, customer address book and automated notifications are not complete.
- Local image uploads need persistent object storage on hosted deployment.

Private project. Add an appropriate license before public distribution.

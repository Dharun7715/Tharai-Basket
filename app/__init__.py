import os
import hmac
import base64
import io
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import qrcode

from flask import Flask, render_template, redirect, url_for, request, flash, session, abort, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(BASE_DIR / 'static' / 'uploads', exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'change-this-development-secret-before-deploying'),
        SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', f"sqlite:///{Path(app.instance_path) / 'tharai_basket.db'}"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        UPLOAD_FOLDER=str(BASE_DIR / 'static' / 'uploads'),
        FREE_DELIVERY_THRESHOLD=Decimal(os.environ.get('FREE_DELIVERY_THRESHOLD', '300')),
        DELIVERY_FEE=Decimal(os.environ.get('DELIVERY_FEE', '25')),
        MIN_ORDER_VALUE=Decimal(os.environ.get('MIN_ORDER_VALUE', '100')),
        DELIVERY_CENTER_LAT=float(os.environ.get('DELIVERY_CENTER_LAT', '11.69522')),
        DELIVERY_CENTER_LON=float(os.environ.get('DELIVERY_CENTER_LON', '77.96991')),
        DELIVERY_RADIUS_KM=float(os.environ.get('DELIVERY_RADIUS_KM', '5')),
        CURRENCY='₹',
    )
    # Render commonly supplies postgres://; SQLAlchemy expects postgresql://.
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
        app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    @app.context_processor
    def inject_globals():
        return {'now': datetime.utcnow(), 'cart_count': sum(int(v) for v in session.get('cart', {}).values()), 'site_name': 'Tharai Basket'}

    @app.template_filter('inr')
    def inr(value):
        try:
            return f"₹{Decimal(str(value)):.2f}"
        except Exception:
            return '₹0.00'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != 'admin':
                abort(403)
            return fn(*args, **kwargs)
        return wrapped

    def delivery_required(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != 'delivery':
                abort(403)
            return fn(*args, **kwargs)
        return wrapped

    def money(value, default='0'):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return Decimal(default)

    # Models are declared inside the factory so Flask-SQLAlchemy binds cleanly.
    class User(UserMixin, db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(120), nullable=False)
        email = db.Column(db.String(180), unique=True, nullable=False, index=True)
        phone = db.Column(db.String(20), default='')
        password_hash = db.Column(db.String(255), nullable=False)
        role = db.Column(db.String(20), default='customer', nullable=False)
        active = db.Column(db.Boolean, default=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        addresses = db.relationship('Address', backref='user', cascade='all, delete-orphan')

        def set_password(self, password):
            self.password_hash = generate_password_hash(password)

        def check_password(self, password):
            return check_password_hash(self.password_hash, password)

    class Address(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        label = db.Column(db.String(40), default='Home')
        recipient = db.Column(db.String(120), nullable=False)
        phone = db.Column(db.String(20), nullable=False)
        line1 = db.Column(db.String(220), nullable=False)
        landmark = db.Column(db.String(160), default='')
        pincode = db.Column(db.String(10), default='')

    class Category(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(100), unique=True, nullable=False)
        slug = db.Column(db.String(120), unique=True, nullable=False)
        image = db.Column(db.String(255), default='')
        active = db.Column(db.Boolean, default=True)

    class Product(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(180), nullable=False)
        brand = db.Column(db.String(100), default='')
        description = db.Column(db.Text, default='')
        unit = db.Column(db.String(40), default='1 unit')
        category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
        mrp = db.Column(db.Numeric(10, 2), nullable=False, default=0)
        price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
        stock = db.Column(db.Integer, default=0)
        image = db.Column(db.String(255), default='')
        active = db.Column(db.Boolean, default=True)
        featured = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        category = db.relationship('Category', backref='products')

        @property
        def discount_percent(self):
            if self.mrp and self.mrp > self.price and self.mrp > 0:
                return int((self.mrp - self.price) * 100 / self.mrp)
            return 0

    class WishlistItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User', backref=db.backref('wishlist_items', cascade='all, delete-orphan'))
        product = db.relationship('Product')

        __table_args__ = (db.UniqueConstraint('user_id', 'product_id', name='uq_wishlist_user_product'),)

    class Supplier(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(150), nullable=False)
        contact_person = db.Column(db.String(120), default='')
        phone = db.Column(db.String(20), default='')
        address = db.Column(db.Text, default='')
        notes = db.Column(db.Text, default='')
        active = db.Column(db.Boolean, default=True)

    class SupplierProduct(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)
        product_name = db.Column(db.String(180), nullable=False)
        unit = db.Column(db.String(40), default='1 unit')
        wholesale_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
        min_qty = db.Column(db.Integer, default=1)
        available = db.Column(db.Boolean, default=True)
        supplier = db.relationship('Supplier', backref='items')

    class Order(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        order_number = db.Column(db.String(30), unique=True, nullable=False)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        customer_name = db.Column(db.String(120), nullable=False)
        phone = db.Column(db.String(20), nullable=False)
        address_text = db.Column(db.Text, nullable=False)
        subtotal = db.Column(db.Numeric(10, 2), nullable=False)
        discount = db.Column(db.Numeric(10, 2), default=0)
        delivery_fee = db.Column(db.Numeric(10, 2), default=0)
        total = db.Column(db.Numeric(10, 2), nullable=False)
        payment_method = db.Column(db.String(30), default='COD')
        payment_status = db.Column(db.String(30), default='Pending')
        status = db.Column(db.String(40), default='Pending')
        notes = db.Column(db.Text, default='')
        delivery_partner_id = db.Column(db.Integer, db.ForeignKey('user.id'))
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        customer = db.relationship('User', foreign_keys=[user_id], backref='orders')
        delivery_partner = db.relationship('User', foreign_keys=[delivery_partner_id])
        items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')

    class OrderItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'))
        product_name = db.Column(db.String(180), nullable=False)
        unit = db.Column(db.String(40), default='1 unit')
        quantity = db.Column(db.Integer, nullable=False)
        unit_price = db.Column(db.Numeric(10, 2), nullable=False)
        purchase_cost = db.Column(db.Numeric(10, 2), default=0)

    class Purchase(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
        invoice_number = db.Column(db.String(80), default='')
        total_cost = db.Column(db.Numeric(10, 2), default=0)
        status = db.Column(db.String(30), default='Planned')
        notes = db.Column(db.Text, default='')
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        supplier = db.relationship('Supplier')
        order = db.relationship('Order')

    class PurchaseItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        purchase_id = db.Column(db.Integer, db.ForeignKey('purchase.id'), nullable=False)
        product_name = db.Column(db.String(180), nullable=False)
        quantity = db.Column(db.Integer, default=1)
        unit_cost = db.Column(db.Numeric(10, 2), default=0)
        purchase = db.relationship('Purchase', backref=db.backref('items', cascade='all, delete-orphan'))

    class Coupon(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        code = db.Column(db.String(40), unique=True, nullable=False)
        discount_type = db.Column(db.String(20), default='fixed')
        value = db.Column(db.Numeric(10, 2), default=0)
        min_order = db.Column(db.Numeric(10, 2), default=0)
        active = db.Column(db.Boolean, default=True)

    class DeliveryEarning(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        partner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
        amount = db.Column(db.Numeric(10, 2), nullable=False)
        paid = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        partner = db.relationship('User')
        order = db.relationship('Order')

    # Make model classes available to route handlers and CLI.
    app.db_models = locals()
    UserModel, AddressModel, CategoryModel, ProductModel = User, Address, Category, Product
    SupplierModel, SupplierProductModel, OrderModel, OrderItemModel = Supplier, SupplierProduct, Order, OrderItem
    PurchaseModel, PurchaseItemModel, CouponModel, DeliveryEarningModel = Purchase, PurchaseItem, Coupon, DeliveryEarning

    @app.get('/')
    def index():
        location_verified = bool(session.get('delivery_location'))
        categories = Category.query.filter_by(active=True).order_by(Category.name).all() if location_verified else []
        products = Product.query.filter_by(active=True).order_by(Product.featured.desc(), Product.name).limit(24).all() if location_verified else []
        wishlist_product_ids = set()
        if current_user.is_authenticated and current_user.role == 'customer':
            wishlist_product_ids = {item.product_id for item in WishlistItem.query.filter_by(user_id=current_user.id).all()}
        return render_template('index.html', categories=categories, products=products,
                               location_verified=location_verified,
                               delivery_location=session.get('delivery_location'),
                               wishlist_product_ids=wishlist_product_ids)

    @app.get('/product/<int:product_id>')
    def product_detail(product_id):
        product = Product.query.get_or_404(product_id)
        if not product.active:
            abort(404)
        is_wishlisted = False
        if current_user.is_authenticated and current_user.role == 'customer':
            is_wishlisted = WishlistItem.query.filter_by(user_id=current_user.id, product_id=product.id).first() is not None
        return render_template('product.html', product=product, is_wishlisted=is_wishlisted)

    @app.get('/wishlist')
    @login_required
    def wishlist():
        if current_user.role != 'customer':
            abort(403)
        items = WishlistItem.query.filter_by(user_id=current_user.id).order_by(WishlistItem.created_at.desc()).all()
        return render_template('wishlist.html', items=items)

    @app.post('/wishlist/toggle/<int:product_id>')
    @login_required
    def wishlist_toggle(product_id):
        if current_user.role != 'customer':
            abort(403)
        product = Product.query.filter_by(id=product_id, active=True).first_or_404()
        item = WishlistItem.query.filter_by(user_id=current_user.id, product_id=product.id).first()
        if item:
            db.session.delete(item)
            db.session.commit()
            flash(f'{product.name} removed from your wishlist.', 'info')
        else:
            db.session.add(WishlistItem(user_id=current_user.id, product_id=product.id))
            db.session.commit()
            flash(f'{product.name} saved to your wishlist.', 'success')
        return redirect(request.referrer or url_for('wishlist'))

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            phone = request.form.get('phone', '').strip()
            password = request.form.get('password', '')
            if not name or not email or len(password) < 8:
                flash('Enter your name, valid email and a password with at least 8 characters.', 'danger')
            elif User.query.filter_by(email=email).first():
                flash('An account with this email already exists.', 'warning')
            else:
                user = User(name=name, email=email, phone=phone, role='customer')
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                login_user(user)
                flash('Welcome to Tharai Basket!', 'success')
                return redirect(url_for('index'))
        return render_template('auth.html', mode='register')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            user = User.query.filter_by(email=email, active=True).first()
            if user and user.check_password(request.form.get('password', '')):
                login_user(user)
                if user.role == 'admin':
                    return redirect(url_for('admin_dashboard'))
                if user.role == 'delivery':
                    return redirect(url_for('delivery_dashboard'))
                return redirect(request.args.get('next') or url_for('index'))
            flash('Incorrect email or password.', 'danger')
        return render_template('auth.html', mode='login')

    @app.route('/admin/setup', methods=['GET', 'POST'])
    def admin_setup():
        # One-time setup is available only before the first administrator exists.
        setup_key = os.environ.get('ADMIN_SETUP_KEY', '')
        if not setup_key or User.query.filter_by(role='admin').first():
            abort(404)

        if request.method == 'POST':
            supplied_key = request.form.get('setup_key', '')
            if not hmac.compare_digest(supplied_key, setup_key):
                flash('Invalid setup key.', 'danger')
                return render_template('admin_setup.html')

            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not name or not email or len(password) < 12:
                flash('Enter your name, email and a password with at least 12 characters.', 'danger')
            elif password != confirm_password:
                flash('Passwords do not match.', 'danger')
            elif User.query.filter_by(email=email).first():
                flash('An account with this email already exists. Use a different email.', 'danger')
            else:
                admin = User(name=name, email=email, role='admin', active=True)
                admin.set_password(password)
                db.session.add(admin)
                db.session.commit()
                login_user(admin)
                flash('Administrator account created. Keep your login details safe.', 'success')
                return redirect(url_for('admin_dashboard'))

        return render_template('admin_setup.html')

    @app.get('/logout')
    @login_required
    def logout():
        logout_user()
        flash('You have been logged out.', 'info')
        return redirect(url_for('index'))

    @app.post('/cart/add/<int:product_id>')
    def cart_add(product_id):
        product = Product.query.get_or_404(product_id)
        if not product.active or product.stock <= 0:
            flash('This product is currently unavailable.', 'warning')
            return redirect(request.referrer or url_for('index'))
        cart = session.get('cart', {})
        key = str(product_id)
        cart[key] = min(int(cart.get(key, 0)) + max(1, int(request.form.get('quantity', 1))), product.stock)
        session['cart'] = cart
        flash(f'{product.name} added to your basket.', 'success')
        return redirect(request.referrer or url_for('index'))

    @app.post('/cart/update/<int:product_id>')
    def cart_update(product_id):
        product = Product.query.get_or_404(product_id)
        cart = session.get('cart', {})
        qty = int(request.form.get('quantity', 1))
        if qty <= 0:
            cart.pop(str(product_id), None)
        else:
            cart[str(product_id)] = min(qty, product.stock)
        session['cart'] = cart
        return redirect(url_for('cart'))

    @app.get('/cart/remove/<int:product_id>')
    def cart_remove(product_id):
        cart = session.get('cart', {})
        cart.pop(str(product_id), None)
        session['cart'] = cart
        return redirect(url_for('cart'))

    def cart_data():
        cart = session.get('cart', {})
        rows, subtotal = [], Decimal('0.00')
        for pid, qty in cart.items():
            product = Product.query.get(int(pid))
            if product and product.active and product.stock > 0:
                quantity = min(int(qty), product.stock)
                line = Decimal(str(product.price)) * quantity
                subtotal += line
                rows.append({'product': product, 'quantity': quantity, 'line_total': line})
        return rows, subtotal

    @app.get('/cart')
    def cart():
        rows, subtotal = cart_data()
        threshold = app.config['FREE_DELIVERY_THRESHOLD']
        fee = Decimal('0') if subtotal >= threshold else app.config['DELIVERY_FEE']
        return render_template('cart.html', rows=rows, subtotal=subtotal, delivery_fee=fee, total=subtotal + fee, threshold=threshold)

    @app.get('/api/delivery-check')
    def delivery_check():
        try:
            customer_lat = float(request.args.get('lat', ''))
            customer_lon = float(request.args.get('lon', ''))
            if not (-90 <= customer_lat <= 90 and -180 <= customer_lon <= 180):
                raise ValueError('Invalid coordinates')
        except (TypeError, ValueError):
            return jsonify({'eligible': False, 'error': 'Valid GPS coordinates are required.'}), 400
        lat1 = math.radians(app.config['DELIVERY_CENTER_LAT'])
        lat2 = math.radians(customer_lat)
        dlat = lat2 - lat1
        dlon = math.radians(customer_lon - app.config['DELIVERY_CENTER_LON'])
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance_km = 6371.0 * 2 * math.asin(math.sqrt(haversine))
        eligible = distance_km <= app.config['DELIVERY_RADIUS_KM']
        return jsonify({'eligible': eligible, 'distance_km': round(distance_km, 2), 'radius_km': app.config['DELIVERY_RADIUS_KM']})

    @app.post('/api/set-delivery-location')
    def set_delivery_location():
        try:
            customer_lat = float(request.form.get('lat', ''))
            customer_lon = float(request.form.get('lon', ''))
            if not (-90 <= customer_lat <= 90 and -180 <= customer_lon <= 180):
                raise ValueError('Invalid coordinates')
        except (TypeError, ValueError):
            return jsonify({'eligible': False, 'error': 'Valid GPS coordinates are required.'}), 400
        lat1 = math.radians(app.config['DELIVERY_CENTER_LAT'])
        lat2 = math.radians(customer_lat)
        dlat = lat2 - lat1
        dlon = math.radians(customer_lon - app.config['DELIVERY_CENTER_LON'])
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance_km = 6371.0 * 2 * math.asin(math.sqrt(haversine))
        eligible = distance_km <= app.config['DELIVERY_RADIUS_KM']
        if eligible:
            session['delivery_location'] = {'lat': customer_lat, 'lon': customer_lon,
                                            'distance_km': round(distance_km, 2)}
            return jsonify({'eligible': True, 'distance_km': round(distance_km, 2)})
        session.pop('delivery_location', None)
        return jsonify({'eligible': False, 'distance_km': round(distance_km, 2),
                        'error': 'Delivery is available only within 5 km of Tharamangalam town centre.'}), 403

    @app.get('/change-delivery-location')
    def clear_delivery_location():
        session.pop('delivery_location', None)
        return redirect(url_for('index'))

    @app.route('/checkout', methods=['GET', 'POST'])
    @login_required
    def checkout():
        if current_user.role != 'customer':
            abort(403)
        rows, subtotal = cart_data()
        if not rows:
            flash('Your basket is empty.', 'warning')
            return redirect(url_for('cart'))
        threshold = app.config['FREE_DELIVERY_THRESHOLD']
        fee = Decimal('0') if subtotal >= threshold else app.config['DELIVERY_FEE']
        total = subtotal + fee

        # Create a dynamic UPI QR with this checkout's exact payable amount.
        upi_params = {
            'pa': '8838145515@sbi',
            'pn': 'DHARUN M',
            'am': f'{total:.2f}',
            'cu': 'INR',
            'tn': 'Tharai Basket order payment',
        }
        upi_uri = 'upi://pay?' + urlencode(upi_params)
        qr_image = qrcode.make(upi_uri)
        qr_buffer = io.BytesIO()
        qr_image.save(qr_buffer, format='PNG')
        upi_qr_data = 'data:image/png;base64,' + base64.b64encode(qr_buffer.getvalue()).decode('ascii')

        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            phone = request.form.get('phone', '').strip()
            address = request.form.get('address', '').strip()
            payment = request.form.get('payment_method', 'COD').upper()
            upi_reference = request.form.get('upi_reference', '').strip()
            try:
                customer_lat = float(request.form.get('delivery_lat', ''))
                customer_lon = float(request.form.get('delivery_lon', ''))
                if not (-90 <= customer_lat <= 90 and -180 <= customer_lon <= 180):
                    raise ValueError('Invalid coordinates')
                lat1 = math.radians(app.config['DELIVERY_CENTER_LAT'])
                lat2 = math.radians(customer_lat)
                dlat = lat2 - lat1
                dlon = math.radians(customer_lon - app.config['DELIVERY_CENTER_LON'])
                haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
                distance_km = 6371.0 * 2 * math.asin(math.sqrt(haversine))
            except (TypeError, ValueError):
                distance_km = None
            if payment not in ['COD', 'UPI']:
                abort(400)
            if distance_km is None:
                flash('Please share your current location to check delivery availability.', 'warning')
            elif distance_km > app.config['DELIVERY_RADIUS_KM']:
                flash(f"Sorry, delivery is currently available only within {app.config['DELIVERY_RADIUS_KM']:g} km of Tharamangalam town centre. Your location is {distance_km:.1f} km away.", 'danger')
            elif not all([name, phone, address]):
                flash('Please complete your delivery details.', 'danger')
            elif subtotal < app.config['MIN_ORDER_VALUE']:
                flash(f"Minimum order value is {app.config['CURRENCY']}{app.config['MIN_ORDER_VALUE']}.", 'warning')
            elif payment == 'UPI' and not upi_reference:
                flash('After paying by UPI, enter the UPI transaction reference / UTR.', 'warning')
            else:
                # Re-check stock and calculate all prices on the server.
                for row in rows:
                    if row['product'].stock < row['quantity']:
                        flash(f"Insufficient stock for {row['product'].name}.", 'danger')
                        return redirect(url_for('cart'))
                order = Order(order_number=f"TB{datetime.utcnow():%y%m%d%H%M%S}{current_user.id}", user_id=current_user.id,
                              customer_name=name, phone=phone, address_text=address, subtotal=subtotal,
                              delivery_fee=fee, total=total, payment_method=payment,
                              payment_status='Pending' if payment == 'COD' else 'Awaiting UPI verification',
                              status='Pending',
                              notes=(f'UPI reference submitted by customer: {upi_reference}' if payment == 'UPI' else ''))
                db.session.add(order)
                db.session.flush()
                for row in rows:
                    p = row['product']
                    db.session.add(OrderItem(order_id=order.id, product_id=p.id, product_name=p.name,
                                             unit=p.unit, quantity=row['quantity'], unit_price=p.price))
                    p.stock -= row['quantity']
                db.session.commit()
                session['cart'] = {}
                flash(f'Order {order.order_number} placed successfully. UPI payment will be verified by our team.' if payment == 'UPI' else f'Order {order.order_number} placed successfully. Our team will confirm it shortly.', 'success')
                return redirect(url_for('my_orders'))
        return render_template('checkout.html', rows=rows, subtotal=subtotal, delivery_fee=fee, total=total,
                               upi_qr_data=upi_qr_data, upi_id='8838145515@sbi')
    @app.post('/order/<int:order_id>/buy-again')
    @login_required
    def buy_again(order_id):
        if current_user.role != 'customer':
            abort(403)
        order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
        cart = session.get('cart', {})
        added = 0
        unavailable = []
        for item in order.items:
            if not item.product_id:
                unavailable.append(item.product_name)
                continue
            product = Product.query.get(item.product_id)
            if not product or not product.active or product.stock <= 0:
                unavailable.append(item.product_name)
                continue
            key = str(product.id)
            requested = int(item.quantity)
            current_qty = int(cart.get(key, 0))
            new_qty = min(current_qty + requested, product.stock)
            if new_qty > current_qty:
                cart[key] = new_qty
                added += new_qty - current_qty
            if new_qty < current_qty + requested:
                unavailable.append(product.name)
        session['cart'] = cart
        if added:
            flash(f'{added} item(s) from order {order.order_number} added to your basket.', 'success')
        if unavailable:
            flash('Some items were unavailable or limited by current stock: ' + ', '.join(sorted(set(unavailable))), 'warning')
        if not added and not unavailable:
            flash('No available items found in this order.', 'warning')
        return redirect(url_for('cart'))

    @app.get('/my-orders')
    @login_required
    def my_orders():
        if current_user.role != 'customer':
            abort(403)
        orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
        return render_template('orders.html', orders=orders)

    @app.get('/order/<int:order_id>')
    @login_required
    def order_detail(order_id):
        order = Order.query.get_or_404(order_id)
        if current_user.role != 'admin' and order.user_id != current_user.id and order.delivery_partner_id != current_user.id:
            abort(403)
        return render_template('order_detail.html', order=order)

    @app.get('/admin')
    @admin_required
    def admin_dashboard():
        today = datetime.utcnow().date()
        orders = Order.query.order_by(Order.created_at.desc()).limit(8).all()
        total_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total), 0)).filter(Order.status == 'Delivered').scalar()
        return render_template('admin/dashboard.html', order_count=Order.query.count(), customer_count=User.query.filter_by(role='customer').count(),
                               product_count=Product.query.count(), pending_count=Order.query.filter(Order.status.in_(['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Out for Delivery'])).count(),
                               total_sales=total_sales, orders=orders, low_stock=Product.query.filter(Product.stock <= 5, Product.active.is_(True)).count())

    @app.route('/admin/products', methods=['GET', 'POST'])
    @admin_required
    def admin_products():
        if request.method == 'POST':
            product_id = request.form.get('product_id', type=int)
            product = Product.query.get(product_id) if product_id else Product()
            if not product:
                abort(404)
            product.name = request.form.get('name', '').strip()
            product.brand = request.form.get('brand', '').strip()
            product.description = request.form.get('description', '').strip()
            product.unit = request.form.get('unit', '1 unit').strip()
            product.category_id = request.form.get('category_id', type=int) or None
            product.mrp = money(request.form.get('mrp'))
            product.price = money(request.form.get('price'))
            product.stock = max(0, request.form.get('stock', type=int) or 0)
            product.active = request.form.get('active') == 'on'
            product.featured = request.form.get('featured') == 'on'
            image = request.files.get('image')
            if image and image.filename:
                filename = secure_filename(image.filename)
                ext = Path(filename).suffix.lower()
                if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
                    flash('Upload a JPG, PNG or WEBP image.', 'danger')
                    return redirect(url_for('admin_products'))
                safe_name = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{filename}"
                image.save(Path(app.config['UPLOAD_FOLDER']) / safe_name)
                product.image = f'uploads/{safe_name}'
            if not product.name or product.price <= 0 or product.mrp < product.price:
                flash('Enter a product name, a selling price above zero, and MRP not below selling price.', 'danger')
                return redirect(url_for('admin_products'))
            db.session.add(product)
            db.session.commit()
            flash('Product saved successfully.', 'success')
            return redirect(url_for('admin_products'))
        products = Product.query.order_by(Product.created_at.desc()).all()
        categories = Category.query.order_by(Category.name).all()
        return render_template('admin/products.html', products=products, categories=categories)

    @app.route('/admin/products/<int:product_id>/edit', methods=['GET', 'POST'])
    @admin_required
    def admin_product_edit(product_id):
        product = Product.query.get_or_404(product_id)
        if request.method == 'POST':
            product.name = request.form.get('name', '').strip()
            product.brand = request.form.get('brand', '').strip()
            product.description = request.form.get('description', '').strip()
            product.unit = request.form.get('unit', '1 unit').strip()
            product.category_id = request.form.get('category_id', type=int) or None
            product.mrp = money(request.form.get('mrp'))
            product.price = money(request.form.get('price'))
            product.stock = max(0, request.form.get('stock', type=int) or 0)
            product.active = request.form.get('active') == 'on'
            product.featured = request.form.get('featured') == 'on'
            image = request.files.get('image')
            if image and image.filename:
                filename = secure_filename(image.filename)
                ext = Path(filename).suffix.lower()
                if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
                    flash('Upload a JPG, PNG or WEBP image.', 'danger')
                    return redirect(url_for('admin_product_edit', product_id=product.id))
                safe_name = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{filename}"
                image.save(Path(app.config['UPLOAD_FOLDER']) / safe_name)
                product.image = f'uploads/{safe_name}'
            if not product.name or product.price <= 0 or product.mrp < product.price:
                flash('Enter a product name, a selling price above zero, and MRP not below selling price.', 'danger')
                return redirect(url_for('admin_product_edit', product_id=product.id))
            db.session.commit()
            flash('Product updated successfully.', 'success')
            return redirect(url_for('admin_products'))
        return render_template('admin/product_edit.html', product=product, categories=Category.query.order_by(Category.name).all())

    @app.post('/admin/products/<int:product_id>/delete')
    @admin_required
    def admin_product_delete(product_id):
        product = Product.query.get_or_404(product_id)
        product.active = False
        db.session.commit()
        flash('Product archived. Past orders remain intact.', 'success')
        return redirect(url_for('admin_products'))

    @app.route('/admin/categories', methods=['GET', 'POST'])
    @admin_required
    def admin_categories():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if name:
                slug = '-'.join(name.lower().split())
                if not Category.query.filter_by(slug=slug).first():
                    db.session.add(Category(name=name, slug=slug))
                    db.session.commit()
                    flash('Category added.', 'success')
                else:
                    flash('Category already exists.', 'warning')
            return redirect(url_for('admin_categories'))
        return render_template('admin/categories.html', categories=Category.query.order_by(Category.name).all())

    @app.post('/admin/categories/<int:category_id>/delete')
    @admin_required
    def admin_category_delete(category_id):
        category = Category.query.get_or_404(category_id)
        category.active = False
        db.session.commit()
        flash('Category disabled.', 'success')
        return redirect(url_for('admin_categories'))

    @app.route('/admin/orders', methods=['GET', 'POST'])
    @admin_required
    def admin_orders():
        if request.method == 'POST':
            order = Order.query.get_or_404(request.form.get('order_id', type=int))
            status = request.form.get('status', 'Pending')
            allowed = ['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Out for Delivery','Delivered','Cancelled']
            if status not in allowed:
                abort(400)
            order.status = status
            partner_id = request.form.get('delivery_partner_id', type=int)
            if partner_id:
                partner = User.query.filter_by(id=partner_id, role='delivery', active=True).first()
                if not partner:
                    flash('Select a valid active delivery partner.', 'danger')
                    return redirect(url_for('admin_orders'))
                order.delivery_partner_id = partner.id
            if status == 'Delivered' and order.delivery_partner_id:
                existing = DeliveryEarning.query.filter_by(order_id=order.id).first()
                if not existing:
                    db.session.add(DeliveryEarning(partner_id=order.delivery_partner_id, order_id=order.id, amount=Decimal('25.00')))
            db.session.commit()
            flash('Order updated.', 'success')
            return redirect(url_for('admin_orders'))
        orders = Order.query.order_by(Order.created_at.desc()).all()
        partners = User.query.filter_by(role='delivery', active=True).all()
        return render_template('admin/orders.html', orders=orders, partners=partners,
                               statuses=['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Out for Delivery','Delivered','Cancelled'])

    @app.route('/admin/suppliers', methods=['GET', 'POST'])
    @admin_required
    def admin_suppliers():
        if request.method == 'POST':
            supplier = Supplier(name=request.form.get('name','').strip(), contact_person=request.form.get('contact_person','').strip(),
                                phone=request.form.get('phone','').strip(), address=request.form.get('address','').strip(), notes=request.form.get('notes','').strip())
            if not supplier.name:
                flash('Supplier name is required.', 'danger')
            else:
                db.session.add(supplier)
                db.session.commit()
                flash('Supplier added.', 'success')
            return redirect(url_for('admin_suppliers'))
        return render_template('admin/suppliers.html', suppliers=Supplier.query.order_by(Supplier.name).all())

    @app.post('/admin/suppliers/<int:supplier_id>/products')
    @admin_required
    def admin_supplier_product(supplier_id):
        supplier = Supplier.query.get_or_404(supplier_id)
        item = SupplierProduct(supplier_id=supplier.id, product_name=request.form.get('product_name','').strip(),
                               unit=request.form.get('unit','1 unit'), wholesale_price=money(request.form.get('wholesale_price')),
                               min_qty=max(1, request.form.get('min_qty', type=int) or 1))
        if not item.product_name or item.wholesale_price <= 0:
            flash('Enter a product and valid wholesale price.', 'danger')
        else:
            db.session.add(item)
            db.session.commit()
            flash('Supplier price saved.', 'success')
        return redirect(url_for('admin_suppliers'))

    @app.post('/admin/purchases/<int:purchase_id>/update')
    @admin_required
    def admin_purchase_update(purchase_id):
        purchase = Purchase.query.get_or_404(purchase_id)
        supplier_id = request.form.get('supplier_id', type=int)
        if supplier_id:
            supplier = Supplier.query.get_or_404(supplier_id)
            purchase.supplier_id = supplier.id
        purchase.invoice_number = request.form.get('invoice_number', '').strip()
        purchase.status = request.form.get('status', 'Planned')
        purchase.total_cost = money(request.form.get('total_cost'))
        purchase.notes = request.form.get('notes', '').strip()
        if purchase.status not in ['Planned', 'Purchased', 'Received', 'Cancelled']:
            abort(400)
        if purchase.order and purchase.status in ['Purchased', 'Received']:
            purchase.order.status = 'Purchased' if purchase.status == 'Purchased' else 'Packing'
        db.session.commit()
        flash('Purchase record updated.', 'success')
        return redirect(url_for('admin_purchases'))

    @app.get('/admin/purchases')
    @admin_required
    def admin_purchases():
        orders = Order.query.filter(Order.status.in_(['Confirmed','Purchase Required','Purchased','Packing'])).order_by(Order.created_at.asc()).all()
        purchases = Purchase.query.order_by(Purchase.created_at.desc()).limit(50).all()
        return render_template('admin/purchases.html', orders=orders, purchases=purchases, suppliers=Supplier.query.filter_by(active=True).order_by(Supplier.name).all())

    @app.post('/admin/purchases/create/<int:order_id>')
    @admin_required
    def admin_purchase_create(order_id):
        order = Order.query.get_or_404(order_id)
        purchase = Purchase(order_id=order.id, status='Planned', notes='Generated from customer order')
        db.session.add(purchase)
        db.session.flush()
        for item in order.items:
            db.session.add(PurchaseItem(purchase_id=purchase.id, product_name=item.product_name, quantity=item.quantity, unit_cost=0))
        order.status = 'Purchase Required'
        db.session.commit()
        flash('Purchase list generated. Add supplier and actual costs after purchasing.', 'success')
        return redirect(url_for('admin_purchases'))

    @app.route('/admin/delivery-partners', methods=['GET', 'POST'])
    @admin_required
    def admin_delivery_partners():
        if request.method == 'POST':
            name = request.form.get('name','').strip()
            email = request.form.get('email','').strip().lower()
            password = request.form.get('password','')
            phone = request.form.get('phone','').strip()
            if not name or not email or len(password) < 8 or User.query.filter_by(email=email).first():
                flash('Enter a name, unique email and password of at least 8 characters.', 'danger')
            else:
                user = User(name=name, email=email, phone=phone, role='delivery')
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash('Delivery partner created.', 'success')
            return redirect(url_for('admin_delivery_partners'))
        partners = User.query.filter_by(role='delivery').order_by(User.name).all()
        return render_template('admin/delivery.html', partners=partners)

    @app.get('/delivery')
    @delivery_required
    def delivery_dashboard():
        orders = Order.query.filter_by(delivery_partner_id=current_user.id).order_by(Order.created_at.desc()).all()
        earnings = DeliveryEarning.query.filter_by(partner_id=current_user.id).order_by(DeliveryEarning.created_at.desc()).all()
        return render_template('delivery/dashboard.html', orders=orders, earnings=earnings)

    @app.post('/delivery/order/<int:order_id>/status')
    @delivery_required
    def delivery_status(order_id):
        order = Order.query.filter_by(id=order_id, delivery_partner_id=current_user.id).first_or_404()
        status = request.form.get('status')
        if status not in ['Out for Delivery', 'Delivered']:
            abort(400)
        order.status = status
        if status == 'Delivered' and not DeliveryEarning.query.filter_by(order_id=order.id).first():
            db.session.add(DeliveryEarning(partner_id=current_user.id, order_id=order.id, amount=Decimal('25.00')))
        db.session.commit()
        flash('Delivery status updated.', 'success')
        return redirect(url_for('delivery_dashboard'))

    @app.get('/health')
    def health():
        return jsonify({'status': 'ok', 'service': 'Tharai Basket'})

    @app.cli.command('init-db')
    def init_db():
        db.create_all()
        print('Database tables created.')

    @app.cli.command('create-admin')
    def create_admin():
        import getpass
        email = input('Admin email: ').strip().lower()
        name = input('Admin name: ').strip() or 'Tharai Basket Admin'
        password = getpass.getpass('Admin password (minimum 12 characters): ')
        if len(password) < 12:
            print('Password must be at least 12 characters.')
            return
        if User.query.filter_by(email=email).first():
            print('This email already exists.')
            return
        admin = User(name=name, email=email, role='admin')
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print('Admin account created.')

    @app.cli.command('seed-demo')
    def seed_demo():
        db.create_all()
        if Category.query.first():
            print('Demo data already exists.')
            return
        categories = [Category(name=n, slug='-'.join(n.lower().split())) for n in ['Rice & Grains','Dals & Pulses','Cooking Oils','Vegetables','Fruits','Snacks','Household Essentials']]
        db.session.add_all(categories)
        db.session.flush()
        demo = [
            ('Ponni Boiled Rice','1 kg','65','59','Rice & Grains',35),
            ('Toor Dal','1 kg','145','132','Dals & Pulses',20),
            ('Sunflower Oil','1 L','165','154','Cooking Oils',18),
            ('Sugar','1 kg','52','48','Rice & Grains',40),
            ('Tomato','1 kg','40','32','Vegetables',25),
            ('Banana','1 dozen','60','52','Fruits',15),
            ('Bath Soap','1 pack','45','40','Household Essentials',30),
            ('Mixture','200 g','55','49','Snacks',22),
        ]
        for name, unit, mrp, price, cat, stock in demo:
            db.session.add(Product(name=name, unit=unit, mrp=money(mrp), price=money(price), stock=stock,
                                   category=next(c for c in categories if c.name == cat), active=True, featured=True))
        db.session.commit()
        print('Demo categories and products created. Replace sample prices before launch.')

    with app.app_context():
        db.create_all()

    return app

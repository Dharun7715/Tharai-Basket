import os
import secrets
import hmac
import base64
import io
import math
from datetime import datetime, timedelta
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

    class ProductReview(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        rating = db.Column(db.Integer, nullable=False)
        comment = db.Column(db.Text, default='')
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User')
        product = db.relationship('Product')

        __table_args__ = (db.UniqueConstraint('user_id', 'product_id', name='uq_review_user_product'),)

    class ReferralCode(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
        code = db.Column(db.String(20), nullable=False, unique=True, index=True)
        user = db.relationship('User')

    class Referral(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        referrer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        referred_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        referrer = db.relationship('User', foreign_keys=[referrer_id])
        referred = db.relationship('User', foreign_keys=[referred_id])

    class RewardTransaction(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        referral_id = db.Column(db.Integer, db.ForeignKey('referral.id'), nullable=False)
        points = db.Column(db.Integer, nullable=False)
        description = db.Column(db.String(180), nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User')
        referral = db.relationship('Referral')

        __table_args__ = (db.UniqueConstraint('user_id', 'referral_id', name='uq_reward_user_referral'),)

    class RewardRedemption(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False, unique=True)
        points = db.Column(db.Integer, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User')
        order = db.relationship('Order')

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
        search_query = request.args.get('q', '').strip()[:100]
        category_id = request.args.get('category', type=int)
        min_price = request.args.get('min_price', '').strip()
        max_price = request.args.get('max_price', '').strip()
        sort = request.args.get('sort', 'recommended')
        in_stock = request.args.get('in_stock') == '1'
        products_query = Product.query.filter_by(active=True)
        if search_query:
            pattern = f'%{search_query}%'
            products_query = products_query.filter(db.or_(Product.name.ilike(pattern),
                                                           Product.brand.ilike(pattern),
                                                           Product.description.ilike(pattern)))
        selected_category = None
        if category_id:
            selected_category = Category.query.filter_by(id=category_id, active=True).first()
            if selected_category:
                products_query = products_query.filter_by(category_id=selected_category.id)
            else:
                category_id = None
        try:
            if min_price:
                products_query = products_query.filter(Product.price >= max(Decimal('0'), Decimal(min_price)))
        except (InvalidOperation, TypeError):
            min_price = ''
        try:
            if max_price:
                products_query = products_query.filter(Product.price <= max(Decimal('0'), Decimal(max_price)))
        except (InvalidOperation, TypeError):
            max_price = ''
        if in_stock:
            products_query = products_query.filter(Product.stock > 0)
        sort_options = {
            'recommended': (Product.featured.desc(), Product.name.asc()),
            'price_low': (Product.price.asc(), Product.name.asc()),
            'price_high': (Product.price.desc(), Product.name.asc()),
            'name': (Product.name.asc(),),
            'newest': (Product.created_at.desc(),),
        }
        if sort not in sort_options:
            sort = 'recommended'
        products = products_query.order_by(*sort_options[sort]).all() if location_verified else []
        wishlist_product_ids = set()
        if current_user.is_authenticated and current_user.role == 'customer':
            wishlist_product_ids = {item.product_id for item in WishlistItem.query.filter_by(user_id=current_user.id).all()}
        filters_active = bool(search_query or category_id or min_price or max_price or in_stock or sort != 'recommended')
        return render_template('index.html', categories=categories, products=products,
                               location_verified=location_verified,
                               delivery_location=session.get('delivery_location'),
                               wishlist_product_ids=wishlist_product_ids,
                               search_query=search_query, selected_category_id=category_id,
                               min_price=min_price, max_price=max_price, sort=sort,
                               in_stock=in_stock, filters_active=filters_active)

    @app.get('/product/<int:product_id>')
    def product_detail(product_id):
        product = Product.query.get_or_404(product_id)
        if not product.active:
            abort(404)
        is_wishlisted = False
        if current_user.is_authenticated and current_user.role == 'customer':
            is_wishlisted = WishlistItem.query.filter_by(user_id=current_user.id, product_id=product.id).first() is not None
        reviews = ProductReview.query.filter_by(product_id=product.id).order_by(ProductReview.created_at.desc()).all()
        review_count = len(reviews)
        average_rating = round(sum(r.rating for r in reviews) / review_count, 1) if review_count else 0
        can_review = False
        existing_review = None
        if current_user.is_authenticated and current_user.role == 'customer':
            can_review = (Order.query.join(OrderItem, OrderItem.order_id == Order.id)
                          .filter(Order.user_id == current_user.id,
                                  OrderItem.product_id == product.id,
                                  Order.status == 'Delivered').first()) is not None
            existing_review = ProductReview.query.filter_by(user_id=current_user.id, product_id=product.id).first()
        return render_template('product.html', product=product, is_wishlisted=is_wishlisted,
                               reviews=reviews, review_count=review_count, average_rating=average_rating,
                               can_review=can_review, existing_review=existing_review)

    @app.post('/product/<int:product_id>/review')
    @login_required
    def product_review(product_id):
        if current_user.role != 'customer':
            abort(403)
        product = Product.query.filter_by(id=product_id, active=True).first_or_404()
        eligible = (Order.query.join(OrderItem, OrderItem.order_id == Order.id)
                    .filter(Order.user_id == current_user.id,
                            OrderItem.product_id == product.id,
                            Order.status == 'Delivered').first())
        if not eligible:
            flash('Only customers who received this product can submit a review.', 'warning')
            return redirect(url_for('product_detail', product_id=product.id))
        rating = request.form.get('rating', type=int)
        comment = request.form.get('comment', '').strip()
        if rating not in [1, 2, 3, 4, 5] or len(comment) > 1000:
            flash('Choose a rating from 1 to 5. Review text must be under 1000 characters.', 'danger')
            return redirect(url_for('product_detail', product_id=product.id))
        review = ProductReview.query.filter_by(user_id=current_user.id, product_id=product.id).first()
        if review:
            review.rating = rating
            review.comment = comment
            review.created_at = datetime.utcnow()
            flash('Your review has been updated.', 'success')
        else:
            db.session.add(ProductReview(user_id=current_user.id, product_id=product.id,
                                         rating=rating, comment=comment))
            flash('Thanks! Your review has been submitted.', 'success')
        db.session.commit()
        return redirect(url_for('product_detail', product_id=product.id) + '#reviews')

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
            referral_code = request.form.get('referral_code', '').strip().upper()
            referrer = ReferralCode.query.filter_by(code=referral_code).first() if referral_code else None
            if not name or not email or len(password) < 8:
                flash('Enter your name, valid email and a password with at least 8 characters.', 'danger')
            elif User.query.filter_by(email=email).first():
                flash('An account with this email already exists.', 'warning')
            elif referral_code and (not referrer or referrer.user.role != 'customer'):
                flash('That referral code is invalid. Check it and try again.', 'danger')
            else:
                user = User(name=name, email=email, phone=phone, role='customer')
                user.set_password(password)
                db.session.add(user)
                db.session.flush()
                if referrer and referrer.user_id != user.id:
                    db.session.add(Referral(referrer_id=referrer.user_id, referred_id=user.id))
                db.session.commit()
                login_user(user)
                flash('Welcome to Tharai Basket! Your referral is linked. Rewards unlock after your first delivered order.', 'success')
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

    @app.route('/account', methods=['GET', 'POST'])
    @login_required
    def account():
        if current_user.role != 'customer':
            abort(403)
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            phone = request.form.get('phone', '').strip()
            if not name or len(name) > 120 or len(phone) > 20:
                flash('Enter a valid name and phone number.', 'danger')
            else:
                current_user.name = name
                current_user.phone = phone
                db.session.commit()
                flash('Your profile has been updated.', 'success')
                return redirect(url_for('account'))
        addresses = Address.query.filter_by(user_id=current_user.id).order_by(Address.id.desc()).all()
        referral_code = ReferralCode.query.filter_by(user_id=current_user.id).first()
        if not referral_code:
            for _ in range(5):
                candidate = 'THARAI' + secrets.token_hex(3).upper()
                if not ReferralCode.query.filter_by(code=candidate).first():
                    referral_code = ReferralCode(user_id=current_user.id, code=candidate)
                    db.session.add(referral_code)
                    db.session.commit()
                    break
        reward_transactions = RewardTransaction.query.filter_by(user_id=current_user.id).order_by(RewardTransaction.created_at.desc()).all()
        reward_credits = sum(item.points for item in reward_transactions)
        reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
        reward_points = max(0, reward_credits - int(reward_spent or 0))
        referral_count = Referral.query.filter_by(referrer_id=current_user.id).count()
        return render_template('account.html', addresses=addresses, referral_code=referral_code,
                               reward_transactions=reward_transactions, reward_points=reward_points,
                               referral_count=referral_count)

    @app.post('/account/address/add')
    @login_required
    def account_address_add():
        if current_user.role != 'customer':
            abort(403)
        label = request.form.get('label', 'Home').strip()[:40] or 'Home'
        recipient = request.form.get('recipient', '').strip()
        phone = request.form.get('phone', '').strip()
        line1 = request.form.get('line1', '').strip()
        landmark = request.form.get('landmark', '').strip()
        pincode = request.form.get('pincode', '').strip()
        if not all([recipient, phone, line1]) or len(recipient) > 120 or len(phone) > 20 or len(line1) > 220 or len(landmark) > 160 or len(pincode) > 10:
            flash('Please complete the required address details.', 'danger')
        else:
            db.session.add(Address(user_id=current_user.id, label=label, recipient=recipient,
                                   phone=phone, line1=line1, landmark=landmark, pincode=pincode))
            db.session.commit()
            flash('Delivery address saved.', 'success')
        return redirect(url_for('account'))

    @app.post('/account/address/<int:address_id>/delete')
    @login_required
    def account_address_delete(address_id):
        if current_user.role != 'customer':
            abort(403)
        address = Address.query.filter_by(id=address_id, user_id=current_user.id).first_or_404()
        db.session.delete(address)
        db.session.commit()
        flash('Saved address deleted.', 'info')
        return redirect(url_for('account'))

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

    def active_cart_coupon(subtotal):
        code = session.get('coupon_code')
        if not code:
            return None, Decimal('0.00')
        coupon = Coupon.query.filter_by(code=code, active=True).first()
        if not coupon or subtotal < Decimal(str(coupon.min_order or 0)):
            session.pop('coupon_code', None)
            return None, Decimal('0.00')
        value = Decimal(str(coupon.value or 0))
        if coupon.discount_type == 'percent':
            discount = subtotal * value / Decimal('100')
        else:
            discount = value
        return coupon, min(subtotal, max(Decimal('0.00'), discount)).quantize(Decimal('0.01'))

    @app.post('/api/checkout/quantity/<int:product_id>')
    @login_required
    def checkout_update_quantity(product_id):
        if current_user.role != 'customer':
            abort(403)
        product = Product.query.filter_by(id=product_id, active=True).first_or_404()
        cart = session.get('cart', {})
        key = str(product_id)
        if key not in cart:
            return jsonify({'error': 'This product is no longer in your basket.'}), 404
        try:
            change = int(request.form.get('change', '0'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid quantity change.'}), 400
        if change not in [-1, 1]:
            return jsonify({'error': 'Invalid quantity change.'}), 400
        quantity = int(cart.get(key, 0)) + change
        if quantity <= 0:
            cart.pop(key, None)
            session['cart'] = cart
            if not cart:
                session.pop('coupon_code', None)
                return jsonify({'empty': True, 'redirect': url_for('cart')})
        else:
            if quantity > product.stock:
                return jsonify({'error': f'Only {product.stock} unit(s) are available.'}), 400
            cart[key] = quantity
            session['cart'] = cart

        rows, subtotal = cart_data()
        if not rows:
            session.pop('coupon_code', None)
            return jsonify({'empty': True, 'redirect': url_for('cart')})
        applied_coupon_code = session.get('coupon_code')
        threshold = app.config['FREE_DELIVERY_THRESHOLD']
        fee = Decimal('0.00') if subtotal >= threshold else app.config['DELIVERY_FEE']
        coupon, coupon_discount = active_cart_coupon(subtotal)
        reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
        reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
        available_reward_points = max(0, reward_credits - int(reward_spent or 0))
        reward_discount = min(Decimal(available_reward_points), subtotal) if session.get('use_rewards') else Decimal('0.00')
        discount = min(subtotal, coupon_discount + reward_discount)
        total = max(Decimal('0.00'), subtotal + fee - discount)
        upi_params = {'pa': '8838145515@sbi', 'pn': 'DHARUN M', 'am': f'{total:.2f}',
                      'cu': 'INR', 'tn': 'Tharai Basket order payment'}
        qr = qrcode.make('upi://pay?' + urlencode(upi_params))
        buffer = io.BytesIO()
        qr.save(buffer, format='PNG')
        qr_data = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
        current_row = next((row for row in rows if row['product'].id == product_id), None)
        return jsonify({
            'quantity': current_row['quantity'] if current_row else 0,
            'line_total': f"{current_row['line_total']:.2f}" if current_row else '0.00',
            'subtotal': f'{subtotal:.2f}', 'delivery_fee': f'{fee:.2f}',
            'delivery_label': 'FREE' if fee == 0 else f'₹{fee:.2f}',
            'discount': f'{discount:.2f}', 'coupon_discount': f'{coupon_discount:.2f}',
            'reward_discount': f'{reward_discount:.2f}', 'coupon_code': coupon.code if coupon else None,
            'total': f'{total:.2f}', 'upi_qr_data': qr_data,
            'item_count': sum(int(value) for value in session.get('cart', {}).values()),
            'coupon_message': 'Coupon removed: basket value is below its minimum.' if applied_coupon_code and not coupon else None
        })

    @app.post('/coupon/apply')
    @login_required
    def coupon_apply():
        if current_user.role != 'customer':
            abort(403)
        _, subtotal = cart_data()
        code = request.form.get('coupon_code', '').strip().upper()
        coupon = Coupon.query.filter_by(code=code, active=True).first() if code else None
        if not coupon:
            flash('Invalid or inactive coupon code.', 'danger')
        elif subtotal < Decimal(str(coupon.min_order or 0)):
            flash(f'Add items worth ₹{Decimal(str(coupon.min_order or 0)):.2f} to use this coupon.', 'warning')
        else:
            session['coupon_code'] = coupon.code
            flash(f'Coupon {coupon.code} applied successfully!', 'success')
        return redirect(url_for('cart'))

    @app.post('/coupon/remove')
    @login_required
    def coupon_remove():
        session.pop('coupon_code', None)
        flash('Coupon removed.', 'info')
        return redirect(url_for('cart'))

    @app.get('/cart')
    def cart():
        rows, subtotal = cart_data()
        threshold = app.config['FREE_DELIVERY_THRESHOLD']
        fee = Decimal('0') if subtotal >= threshold else app.config['DELIVERY_FEE']
        coupon, coupon_discount = active_cart_coupon(subtotal)
        reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
        reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
        available_reward_points = max(0, reward_credits - int(reward_spent or 0))
        use_rewards = request.args.get('use_rewards') == '1'
        if request.args.get('use_rewards') in ['0', '1']:
            session['use_rewards'] = use_rewards
        else:
            use_rewards = bool(session.get('use_rewards'))
        reward_discount = min(Decimal(available_reward_points), subtotal) if use_rewards else Decimal('0.00')
        discount = min(subtotal, coupon_discount + reward_discount)
        total = max(Decimal('0.00'), subtotal + fee - discount)
        return render_template('cart.html', rows=rows, subtotal=subtotal, delivery_fee=fee,
                               total=total, threshold=threshold, coupon=coupon, discount=discount)

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
        coupon, discount = active_cart_coupon(subtotal)
        total = max(Decimal('0.00'), subtotal + fee - discount)

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
                # Revalidate coupons and reward balance on the server; never trust browser totals.
                coupon, coupon_discount = active_cart_coupon(subtotal)
                reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
                reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
                available_reward_points = max(0, reward_credits - int(reward_spent or 0))
                reward_discount = min(Decimal(available_reward_points), subtotal) if session.get('use_rewards') else Decimal('0.00')
                discount = min(subtotal, coupon_discount + reward_discount)
                total = max(Decimal('0.00'), subtotal + fee - discount)
                order = Order(order_number=f"TB{datetime.utcnow():%y%m%d%H%M%S}{current_user.id}", user_id=current_user.id,
                              customer_name=name, phone=phone, address_text=address, subtotal=subtotal,
                              discount=discount, delivery_fee=fee, total=total, payment_method=payment,
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
                db.session.flush()
                if reward_discount > 0:
                    points_used = int(reward_discount)
                    db.session.add(RewardRedemption(user_id=current_user.id, order_id=order.id, points=points_used))
                db.session.commit()
                session['cart'] = {}
                session.pop('coupon_code', None)
                session.pop('use_rewards', None)
                flash(f'Order {order.order_number} placed successfully. UPI payment will be verified by our team.' if payment == 'UPI' else f'Order {order.order_number} placed successfully. Our team will confirm it shortly.', 'success')
                return redirect(url_for('my_orders'))
        saved_addresses = Address.query.filter_by(user_id=current_user.id).order_by(Address.id.desc()).all()
        return render_template('checkout.html', rows=rows, subtotal=subtotal, delivery_fee=fee, total=total,
                               coupon=coupon, discount=discount, reward_discount=reward_discount,
                               available_reward_points=available_reward_points, use_rewards=use_rewards,
                               saved_addresses=saved_addresses, upi_qr_data=upi_qr_data, upi_id='8838145515@sbi')
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
        month_start = today.replace(day=1)
        week_start = datetime.combine(today - timedelta(days=6), datetime.min.time())
        delivered_query = Order.query.filter_by(status='Delivered')
        total_sales = db.session.query(db.func.coalesce(db.func.sum(Order.total), 0)).filter(Order.status == 'Delivered').scalar()
        monthly_revenue = db.session.query(db.func.coalesce(db.func.sum(Order.total), 0)).filter(
            Order.status == 'Delivered', Order.created_at >= datetime.combine(month_start, datetime.min.time())).scalar()
        today_revenue = db.session.query(db.func.coalesce(db.func.sum(Order.total), 0)).filter(
            Order.status == 'Delivered', Order.created_at >= datetime.combine(today, datetime.min.time())).scalar()
        week_orders = Order.query.filter(Order.created_at >= week_start).all()
        daily_sales = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            amount = sum((Decimal(str(order.total or 0)) for order in week_orders
                          if order.status == 'Delivered' and order.created_at.date() == day), Decimal('0.00'))
            daily_sales.append({'label': day.strftime('%a'), 'date': day.strftime('%d %b'), 'amount': float(amount)})
        max_daily_sales = max([item['amount'] for item in daily_sales] or [0]) or 1
        top_product_totals = {}
        for order in delivered_query.order_by(Order.created_at.desc()).limit(500).all():
            for item in order.items:
                key = item.product_name
                if key not in top_product_totals:
                    top_product_totals[key] = {'name': key, 'quantity': 0, 'revenue': Decimal('0.00')}
                top_product_totals[key]['quantity'] += int(item.quantity or 0)
                top_product_totals[key]['revenue'] += Decimal(str(item.unit_price or 0)) * int(item.quantity or 0)
        top_products = sorted(top_product_totals.values(), key=lambda item: item['quantity'], reverse=True)[:5]
        status_rows = db.session.query(Order.status, db.func.count(Order.id)).group_by(Order.status).all()
        status_breakdown = [{'label': label, 'count': count} for label, count in status_rows]
        payment_rows = db.session.query(Order.payment_method, db.func.count(Order.id)).group_by(Order.payment_method).all()
        payment_breakdown = [{'label': label or 'Other', 'count': count} for label, count in payment_rows]
        recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
        return render_template('admin/dashboard.html',
            order_count=Order.query.count(), customer_count=User.query.filter_by(role='customer').count(),
            product_count=Product.query.filter_by(active=True).count(),
            pending_count=Order.query.filter(Order.status.in_(['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Out for Delivery'])).count(),
            total_sales=total_sales, monthly_revenue=monthly_revenue, today_revenue=today_revenue,
            delivered_count=delivered_query.count(), low_stock=Product.query.filter(Product.stock <= 5, Product.active.is_(True)).count(),
            daily_sales=daily_sales, max_daily_sales=max_daily_sales, top_products=top_products,
            status_breakdown=status_breakdown, payment_breakdown=payment_breakdown, orders=recent_orders)

    @app.route('/admin/coupons', methods=['GET', 'POST'])
    @admin_required
    def admin_coupons():
        if request.method == 'POST':
            code = request.form.get('code', '').strip().upper()
            discount_type = request.form.get('discount_type', 'fixed')
            value = money(request.form.get('value'))
            min_order = money(request.form.get('min_order'))
            if not code or discount_type not in ['fixed', 'percent'] or value <= 0 or min_order < 0:
                flash('Enter a valid code, discount and minimum order value.', 'danger')
            elif discount_type == 'percent' and value > 100:
                flash('Percentage discount cannot exceed 100%.', 'danger')
            elif Coupon.query.filter_by(code=code).first():
                flash('That coupon code already exists.', 'warning')
            else:
                db.session.add(Coupon(code=code, discount_type=discount_type, value=value, min_order=min_order, active=True))
                db.session.commit()
                flash(f'Coupon {code} created.', 'success')
            return redirect(url_for('admin_coupons'))
        coupons = Coupon.query.order_by(Coupon.id.desc()).all()
        return render_template('admin/coupons.html', coupons=coupons)

    @app.post('/admin/coupons/<int:coupon_id>/toggle')
    @admin_required
    def admin_coupon_toggle(coupon_id):
        coupon = Coupon.query.get_or_404(coupon_id)
        coupon.active = not coupon.active
        db.session.commit()
        flash(f'Coupon {coupon.code} is now {"active" if coupon.active else "inactive"}.', 'success')
        return redirect(url_for('admin_coupons'))

    @app.get('/admin/inventory')
    @admin_required
    def admin_inventory():
        products = Product.query.filter_by(active=True).filter(Product.stock <= 5).order_by(Product.stock.asc(), Product.name.asc()).all()
        out_of_stock_count = Product.query.filter_by(active=True, stock=0).count()
        low_stock_count = Product.query.filter(Product.active.is_(True), Product.stock.between(1, 5)).count()
        return render_template('admin/inventory.html', products=products,
                               out_of_stock_count=out_of_stock_count, low_stock_count=low_stock_count)

    @app.post('/admin/inventory/<int:product_id>/stock')
    @admin_required
    def admin_inventory_update_stock(product_id):
        product = Product.query.get_or_404(product_id)
        try:
            stock = int(request.form.get('stock', ''))
        except (TypeError, ValueError):
            flash('Enter a valid whole-number stock quantity.', 'danger')
            return redirect(url_for('admin_inventory'))
        if stock < 0 or stock > 1000000:
            flash('Stock must be between 0 and 1,000,000.', 'danger')
            return redirect(url_for('admin_inventory'))
        product.stock = stock
        db.session.commit()
        flash(f'Stock updated for {product.name}: {stock} unit(s).', 'success')
        return redirect(url_for('admin_inventory'))

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

    def award_referral_points(order):
        if order.status != 'Delivered':
            return
        referral = Referral.query.filter_by(referred_id=order.user_id).first()
        if not referral:
            return
        if RewardTransaction.query.filter_by(user_id=referral.referrer_id, referral_id=referral.id).first():
            return
        db.session.add(RewardTransaction(user_id=referral.referrer_id, referral_id=referral.id,
                                          points=50, description='Referral reward for a friend’s first delivered order'))
        db.session.add(RewardTransaction(user_id=referral.referred_id, referral_id=referral.id,
                                          points=50, description='Welcome reward for your first delivered order'))

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
            award_referral_points(order)
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
        all_orders = Order.query.filter_by(delivery_partner_id=current_user.id).order_by(Order.created_at.desc()).all()
        active_orders = [o for o in all_orders if o.status in ['Ready for Pickup', 'Out for Delivery']]
        completed_orders = [o for o in all_orders if o.status == 'Delivered']
        earnings = DeliveryEarning.query.filter_by(partner_id=current_user.id).order_by(DeliveryEarning.created_at.desc()).all()
        total_earnings = sum((Decimal(str(e.amount or 0)) for e in earnings), Decimal('0.00'))
        pending_earnings = sum((Decimal(str(e.amount or 0)) for e in earnings if not e.paid), Decimal('0.00'))
        return render_template('delivery/dashboard.html', orders=all_orders, active_orders=active_orders,
                               completed_orders=completed_orders, earnings=earnings,
                               total_earnings=total_earnings, pending_earnings=pending_earnings)

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
        award_referral_points(order)
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
        # Ensure the launch offer exists without changing existing customer/order data.
        default_coupon = Coupon.query.filter_by(code='THARAI50').first()
        if not default_coupon:
            db.session.add(Coupon(code='THARAI50', discount_type='fixed', value=Decimal('50.00'),
                                  min_order=Decimal('500.00'), active=True))
            db.session.commit()

    return app

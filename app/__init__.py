import os
import secrets
import hmac
import base64
import io
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
        unread_notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count() if current_user.is_authenticated and current_user.role == 'customer' else 0
        vip_theme_tier = None
        if current_user.is_authenticated and current_user.role == 'customer':
            active_theme_membership = VipMembership.query.filter_by(user_id=current_user.id, active=True).filter(
                VipMembership.ends_at > datetime.utcnow()
            ).order_by(VipMembership.ends_at.desc()).first()
            vip_theme_tier = active_theme_membership.tier if active_theme_membership and active_theme_membership.tier in ('Gold', 'Platinum') else None
        return {'now': datetime.utcnow(), 'cart_count': sum(int(v) for v in session.get('cart', {}).values()), 'site_name': 'Tharai Basket', 'unread_notifications': unread_notifications, 'vip_theme_tier': vip_theme_tier}

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

    class ProductGalleryImage(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        image_path = db.Column(db.String(255), nullable=False)
        alt_text = db.Column(db.String(180), default='')
        sort_order = db.Column(db.Integer, default=0)
        product = db.relationship('Product', backref=db.backref('gallery_images', cascade='all, delete-orphan', order_by='ProductGalleryImage.sort_order'))

    class ComboOffer(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        title = db.Column(db.String(140), nullable=False)
        description = db.Column(db.String(500), default='')
        product_ids = db.Column(db.Text, nullable=False, default='[]')
        combo_price = db.Column(db.Numeric(10, 2), nullable=False)
        active = db.Column(db.Boolean, default=True, nullable=False)
        starts_at = db.Column(db.DateTime)
        ends_at = db.Column(db.DateTime)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class FlashSale(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        sale_price = db.Column(db.Numeric(10, 2), nullable=False)
        starts_at = db.Column(db.DateTime, nullable=False)
        ends_at = db.Column(db.DateTime, nullable=False)
        active = db.Column(db.Boolean, default=True, nullable=False)
        product = db.relationship('Product')

    class StockBatch(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        received_quantity = db.Column(db.Integer, nullable=False)
        remaining_quantity = db.Column(db.Integer, nullable=False)
        expiry_date = db.Column(db.String(10), nullable=False, index=True)
        received_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        product = db.relationship('Product')

    class VipMembership(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        tier = db.Column(db.String(20), nullable=False, default='Silver')
        starts_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        ends_at = db.Column(db.DateTime, nullable=False)
        active = db.Column(db.Boolean, default=True, nullable=False)
        user = db.relationship('User')

    class MembershipPayment(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        tier = db.Column(db.String(20), nullable=False)
        months = db.Column(db.Integer, nullable=False, default=1)
        amount = db.Column(db.Numeric(10, 2), nullable=False)
        upi_reference = db.Column(db.String(100), nullable=False)
        status = db.Column(db.String(20), nullable=False, default='Pending', index=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        reviewed_at = db.Column(db.DateTime)
        reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        user = db.relationship('User', foreign_keys=[user_id])
        reviewer = db.relationship('User', foreign_keys=[reviewed_by])

    class ShoppingListItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False, index=True)
        quantity = db.Column(db.Integer, nullable=False, default=1)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User')
        product = db.relationship('Product')
        __table_args__ = (db.UniqueConstraint('user_id', 'product_id', name='uq_shopping_list_user_product'),)

    class GrocerySubscription(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        title = db.Column(db.String(120), nullable=False, default='My grocery repeat')
        items_json = db.Column(db.Text, nullable=False, default='{}')
        frequency = db.Column(db.String(20), nullable=False, default='Weekly')
        next_date = db.Column(db.String(10), nullable=False)
        active = db.Column(db.Boolean, nullable=False, default=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow)
        user = db.relationship('User')

    class WalletTransaction(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        amount = db.Column(db.Numeric(10, 2), nullable=False)
        kind = db.Column(db.String(20), nullable=False)
        reference = db.Column(db.String(80), default='')
        description = db.Column(db.String(180), nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        user = db.relationship('User')

    class GiftCard(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        code = db.Column(db.String(32), unique=True, nullable=False, index=True)
        amount = db.Column(db.Numeric(10, 2), nullable=False)
        status = db.Column(db.String(20), nullable=False, default='Active')
        created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
        redeemed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        redeemed_at = db.Column(db.DateTime)
        creator = db.relationship('User', foreign_keys=[created_by])
        redeemer = db.relationship('User', foreign_keys=[redeemed_by])

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

    class LoyaltyPoint(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False, unique=True)
        points = db.Column(db.Integer, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
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

    class DeliverySchedule(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False, unique=True)
        delivery_date = db.Column(db.String(10), nullable=False)
        delivery_slot = db.Column(db.String(20), nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        order = db.relationship('Order', backref=db.backref('delivery_schedule', uselist=False))

    class AbandonedCart(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        cart_data = db.Column(db.Text, nullable=False, default='{}')
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
        recovered_at = db.Column(db.DateTime)
        user = db.relationship('User', backref='abandoned_carts')

    class DeliverySlotSetting(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        capacity = db.Column(db.Integer, nullable=False, default=5)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    class StoreExpense(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        title = db.Column(db.String(180), nullable=False)
        category = db.Column(db.String(80), nullable=False, default='Other')
        amount = db.Column(db.Numeric(10, 2), nullable=False)
        expense_date = db.Column(db.String(10), nullable=False)
        notes = db.Column(db.Text, default='')
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    class Notification(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
        title = db.Column(db.String(140), nullable=False)
        message = db.Column(db.String(500), nullable=False)
        category = db.Column(db.String(40), default='order')
        is_read = db.Column(db.Boolean, default=False, nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        user = db.relationship('User', backref='notifications')
        order = db.relationship('Order', backref='notifications')

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

    def available_delivery_slots(date_value, order_time=None):
        local_now = datetime.now(ZoneInfo('Asia/Kolkata'))
        try:
            selected_date = datetime.strptime(date_value, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return []
        today = local_now.date()
        if selected_date < today or selected_date > today + timedelta(days=7):
            return []
        if selected_date == today:
            base_time = order_time or local_now.replace(tzinfo=None)
            first = base_time + timedelta(hours=2)
            # Round UP to the next half-hour boundary, never earlier than +2 hours.
            minute_remainder = first.minute % 30
            if minute_remainder or first.second or first.microsecond:
                first += timedelta(minutes=(30 - minute_remainder) if minute_remainder else 30)
            first = first.replace(second=0, microsecond=0)
        else:
            first = datetime.combine(selected_date, datetime.strptime('08:00', '%H:%M').time())
        end = datetime.combine(selected_date, datetime.strptime('22:00', '%H:%M').time())
        setting = DeliverySlotSetting.query.first()
        capacity = max(1, int(setting.capacity if setting else 5))
        slots = []
        cursor = first
        while cursor.date() == selected_date and cursor <= end:
            slot = cursor.strftime('%I:%M %p')
            booked = DeliverySchedule.query.join(Order).filter(
                DeliverySchedule.delivery_date == date_value,
                DeliverySchedule.delivery_slot == slot,
                Order.status != 'Cancelled'
            ).count()
            if booked < capacity:
                slots.append(slot)
            cursor += timedelta(minutes=30)
        return slots

    @app.get('/api/delivery-slots')
    @login_required
    def delivery_slots_api():
        if current_user.role != 'customer':
            abort(403)
        date_value = request.args.get('date', '')
        return jsonify({'date': date_value, 'slots': available_delivery_slots(date_value),
                        'capacity_per_slot': max(1, int((DeliverySlotSetting.query.first() or DeliverySlotSetting(capacity=5)).capacity))})

    def create_customer_notification(order, title, message, category='order'):
        db.session.add(Notification(user_id=order.user_id, order_id=order.id,
                                    title=title, message=message, category=category))

    def award_order_loyalty_points(order):
        if order.status != 'Delivered' or LoyaltyPoint.query.filter_by(order_id=order.id).first():
            return
        points = int(Decimal(str(order.subtotal or 0)) // Decimal('100'))
        if points > 0:
            db.session.add(LoyaltyPoint(user_id=order.user_id, order_id=order.id, points=points))
            create_customer_notification(order, 'Loyalty points earned',
                f'You earned {points} Tharai points for this delivered order. 100 points = ₹100 reward value at checkout.', 'rewards')

    def wallet_balance(user_id):
        credits = db.session.query(db.func.coalesce(db.func.sum(WalletTransaction.amount), 0)).filter_by(user_id=user_id, kind='Credit').scalar()
        debits = db.session.query(db.func.coalesce(db.func.sum(WalletTransaction.amount), 0)).filter_by(user_id=user_id, kind='Debit').scalar()
        return max(Decimal('0.00'), Decimal(str(credits or 0)) - Decimal(str(debits or 0))).quantize(Decimal('0.01'))

    @app.get('/account/tools')
    @login_required
    def account_tools():
        if current_user.role != 'customer':
            abort(403)
        items = ShoppingListItem.query.filter_by(user_id=current_user.id).order_by(ShoppingListItem.created_at.desc()).all()
        subscriptions = GrocerySubscription.query.filter_by(user_id=current_user.id).order_by(GrocerySubscription.next_date.asc()).all()
        transactions = WalletTransaction.query.filter_by(user_id=current_user.id).order_by(WalletTransaction.created_at.desc()).limit(30).all()
        today_date = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
        loyalty_points = sum(row.points for row in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
        referral_points = sum(row.points for row in RewardTransaction.query.filter_by(user_id=current_user.id).all())
        redeemed_points = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar() or 0
        return render_template('account_tools.html', items=items, subscriptions=subscriptions, today_date=today_date,
                               wallet_balance=wallet_balance(current_user.id), transactions=transactions,
                               loyalty_points=max(0, loyalty_points + referral_points - int(redeemed_points)))

    @app.post('/account/shopping-list/add/<int:product_id>')
    @login_required
    def shopping_list_add(product_id):
        if current_user.role != 'customer':
            abort(403)
        product = Product.query.filter_by(id=product_id, active=True).first_or_404()
        qty = max(1, min(99, request.form.get('quantity', 1, type=int) or 1))
        item = ShoppingListItem.query.filter_by(user_id=current_user.id, product_id=product.id).first()
        if item:
            item.quantity = min(99, item.quantity + qty)
        else:
            db.session.add(ShoppingListItem(user_id=current_user.id, product_id=product.id, quantity=qty))
        db.session.commit()
        flash(f'{product.name} saved to your shopping list.', 'success')
        return redirect(request.referrer or url_for('account_tools'))

    @app.post('/account/shopping-list/<int:item_id>/remove')
    @login_required
    def shopping_list_remove(item_id):
        item = ShoppingListItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
        db.session.delete(item)
        db.session.commit()
        flash('Item removed from your shopping list.', 'info')
        return redirect(url_for('account_tools'))

    @app.post('/account/shopping-list/add-all')
    @login_required
    def shopping_list_add_all():
        if current_user.role != 'customer':
            abort(403)
        cart = session.get('cart', {})
        added = 0
        for item in ShoppingListItem.query.filter_by(user_id=current_user.id).all():
            product = item.product
            if product and product.active and product.stock > 0:
                key = str(product.id)
                before = int(cart.get(key, 0))
                cart[key] = min(product.stock, before + item.quantity)
                added += max(0, cart[key] - before)
        session['cart'] = cart
        save_customer_cart_snapshot()
        flash(f'{added} item(s) from your shopping list added to the basket.', 'success' if added else 'warning')
        return redirect(url_for('cart'))

    @app.post('/account/subscriptions/create')
    @login_required
    def subscription_create():
        if current_user.role != 'customer':
            abort(403)
        frequency = request.form.get('frequency', 'Weekly')
        if frequency not in ['Weekly', 'Fortnightly', 'Monthly']:
            abort(400)
        cart = session.get('cart', {})
        valid = {}
        for pid, qty in cart.items():
            product = Product.query.filter_by(id=int(pid), active=True).first()
            if product and product.stock > 0:
                valid[str(product.id)] = min(99, max(1, int(qty)))
        if not valid:
            flash('Add available products to your basket before creating a subscription.', 'warning')
            return redirect(url_for('cart'))
        local_today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        delta = {'Weekly': 7, 'Fortnightly': 14, 'Monthly': 30}[frequency]
        title = request.form.get('title', '').strip()[:120] or f'{frequency} grocery basket'
        db.session.add(GrocerySubscription(user_id=current_user.id, title=title, items_json=__import__('json').dumps(valid),
                                           frequency=frequency, next_date=(local_today + timedelta(days=delta)).isoformat()))
        db.session.commit()
        flash('Repeat basket saved. We will remind you when it is due; no payment or order is created automatically.', 'success')
        return redirect(url_for('account_tools'))

    @app.post('/account/subscriptions/<int:subscription_id>/toggle')
    @login_required
    def subscription_toggle(subscription_id):
        sub = GrocerySubscription.query.filter_by(id=subscription_id, user_id=current_user.id).first_or_404()
        sub.active = not sub.active
        db.session.commit()
        return redirect(url_for('account_tools'))

    @app.post('/account/subscriptions/<int:subscription_id>/add-to-cart')
    @login_required
    def subscription_add_to_cart(subscription_id):
        sub = GrocerySubscription.query.filter_by(id=subscription_id, user_id=current_user.id, active=True).first_or_404()
        cart = session.get('cart', {})
        added = 0
        try:
            planned = __import__('json').loads(sub.items_json)
        except (ValueError, TypeError):
            planned = {}
        for pid, qty in planned.items():
            product = Product.query.filter_by(id=int(pid), active=True).first()
            if product and product.stock > 0:
                key = str(product.id)
                before = int(cart.get(key, 0))
                cart[key] = min(product.stock, before + int(qty))
                added += max(0, cart[key] - before)
        session['cart'] = cart
        session['pending_subscription_id'] = sub.id
        save_customer_cart_snapshot()
        flash(f'{added} subscription item(s) added to basket. Review quantities and checkout when ready.', 'success' if added else 'warning')
        return redirect(url_for('cart'))

    @app.post('/account/gift-cards/redeem')
    @login_required
    def gift_card_redeem():
        if current_user.role != 'customer':
            abort(403)
        code = request.form.get('code', '').strip().upper()
        card = GiftCard.query.filter_by(code=code, status='Active').with_for_update().first()
        if not card:
            flash('Gift card is invalid or has already been redeemed.', 'danger')
            return redirect(url_for('account_tools'))
        card.status = 'Redeemed'
        card.redeemed_by = current_user.id
        card.redeemed_at = datetime.utcnow()
        db.session.add(WalletTransaction(user_id=current_user.id, amount=card.amount, kind='Credit',
                                         reference=card.code, description='Gift card redeemed'))
        db.session.commit()
        flash(f'Gift card redeemed. ₹{card.amount:.2f} added to your wallet.', 'success')
        return redirect(url_for('account_tools'))

    @app.get('/notifications')
    @login_required
    def notifications():
        if current_user.role != 'customer':
            abort(403)
        items = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(100).all()
        Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
        db.session.commit()
        return render_template('notifications.html', notifications=items)

    @app.get('/')
    def index():
        location_verified = bool(session.get('delivery_location'))
        show_location_picker = request.args.get('choose_location') == '1'
        categories = Category.query.filter_by(active=True).order_by(Category.name).all()
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
        products = products_query.order_by(*sort_options[sort]).all()
        recently_viewed = []
        if not search_query and not category_id and not min_price and not max_price and not in_stock:
            viewed_ids = session.get('recently_viewed_products', [])
            if viewed_ids:
                viewed_map = {p.id: p for p in Product.query.filter(
                    Product.id.in_(viewed_ids), Product.active.is_(True), Product.stock > 0
                ).all()}
                recently_viewed = [viewed_map[pid] for pid in viewed_ids if pid in viewed_map][:6]
        recommended_products = []
        active_flash_sales = []
        active_combo_offers = []
        vip_membership = None
        if location_verified:
            if current_user.is_authenticated and current_user.role == 'customer':
                bought = [row[0] for row in db.session.query(OrderItem.product_id).join(
                    Order, Order.id == OrderItem.order_id).filter(
                    Order.user_id == current_user.id, Order.status == 'Delivered',
                    OrderItem.product_id.isnot(None)).distinct().limit(30).all()]
                cats = [row[0] for row in db.session.query(Product.category_id).filter(
                    Product.id.in_(bought), Product.category_id.isnot(None)).distinct().all()] if bought else []
                rq = Product.query.filter(Product.active.is_(True), Product.stock > 0)
                if bought: rq = rq.filter(~Product.id.in_(bought))
                if cats: rq = rq.filter(Product.category_id.in_(cats))
                recommended_products = rq.order_by(Product.featured.desc(), Product.created_at.desc()).limit(4).all()
                if not recommended_products:
                    popular_ids = [row[0] for row in db.session.query(
                        OrderItem.product_id
                    ).join(Order, Order.id == OrderItem.order_id).filter(
                        Order.status == 'Delivered',
                        Order.created_at >= datetime.utcnow() - timedelta(days=30),
                        OrderItem.product_id.isnot(None)
                    ).group_by(OrderItem.product_id).order_by(
                        db.func.sum(OrderItem.quantity).desc()
                    ).limit(8).all()]
                    popular_map = {p.id: p for p in Product.query.filter(
                        Product.id.in_(popular_ids), Product.active.is_(True), Product.stock > 0
                    ).all()} if popular_ids else {}
                    recommended_products = [popular_map[pid] for pid in popular_ids if pid in popular_map][:4]
                if len(recommended_products) < 4:
                    existing_ids = [p.id for p in recommended_products]
                    fill_query = Product.query.filter(Product.active.is_(True), Product.stock > 0)
                    if existing_ids:
                        fill_query = fill_query.filter(~Product.id.in_(existing_ids))
                    recommended_products.extend(fill_query.order_by(Product.featured.desc(), Product.created_at.desc()).limit(4-len(recommended_products)).all())
                vip_membership = VipMembership.query.filter_by(user_id=current_user.id, active=True).filter(
                    VipMembership.ends_at > datetime.utcnow()).order_by(VipMembership.ends_at.desc()).first()
            now_utc = datetime.utcnow()
            active_flash_sales = FlashSale.query.join(Product).filter(
                FlashSale.active.is_(True), FlashSale.starts_at <= now_utc, FlashSale.ends_at > now_utc,
                Product.active.is_(True), Product.stock > 0).order_by(FlashSale.ends_at.asc()).limit(8).all()
            active_combo_offers = ComboOffer.query.filter(
                ComboOffer.active.is_(True),
                db.or_(ComboOffer.starts_at.is_(None), ComboOffer.starts_at <= now_utc),
                db.or_(ComboOffer.ends_at.is_(None), ComboOffer.ends_at > now_utc)).order_by(ComboOffer.created_at.desc()).limit(4).all()
        wishlist_product_ids = set()
        if current_user.is_authenticated and current_user.role == 'customer':
            wishlist_product_ids = {item.product_id for item in WishlistItem.query.filter_by(user_id=current_user.id).all()}
        filters_active = bool(search_query or category_id or min_price or max_price or in_stock or sort != 'recommended')
        return render_template('index.html', categories=categories, products=products,
                               show_location_picker=show_location_picker,
                               location_verified=location_verified,
                               delivery_location=session.get('delivery_location'),
                               wishlist_product_ids=wishlist_product_ids,
                               search_query=search_query, selected_category_id=category_id,
                               min_price=min_price, max_price=max_price, sort=sort,
                               in_stock=in_stock, filters_active=filters_active, recently_viewed=recently_viewed,
                                recommended_products=recommended_products, active_flash_sales=active_flash_sales,
                                active_combo_offers=active_combo_offers, vip_membership=vip_membership)

    @app.get('/product/<int:product_id>')
    def product_detail(product_id):
        product = Product.query.get_or_404(product_id)
        if not product.active:
            abort(404)
        viewed_ids = session.get('recently_viewed_products', [])
        viewed_ids = [int(pid) for pid in viewed_ids if str(pid).isdigit() and int(pid) != product.id]
        session['recently_viewed_products'] = ([product.id] + viewed_ids)[:8]
        is_wishlisted = False
        if current_user.is_authenticated and current_user.role == 'customer':
            is_wishlisted = WishlistItem.query.filter_by(user_id=current_user.id, product_id=product.id).first() is not None
        gallery_images = ProductGalleryImage.query.filter_by(product_id=product.id).order_by(ProductGalleryImage.sort_order.asc()).all()
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
                               can_review=can_review, existing_review=existing_review, gallery_images=gallery_images)

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
        reward_credits += sum(item.points for item in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
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

    @app.post('/combo/<int:combo_id>/add')
    def combo_add_to_cart(combo_id):
        now_utc = datetime.utcnow()
        combo = ComboOffer.query.filter_by(id=combo_id, active=True).filter(
            db.or_(ComboOffer.starts_at.is_(None), ComboOffer.starts_at <= now_utc),
            db.or_(ComboOffer.ends_at.is_(None), ComboOffer.ends_at > now_utc)
        ).first_or_404()
        try:
            ids = list(dict.fromkeys(int(x) for x in __import__('json').loads(combo.product_ids)))
        except (ValueError, TypeError):
            abort(404)
        products = Product.query.filter(Product.id.in_(ids), Product.active.is_(True)).all()
        if len(ids) < 2 or len(products) != len(ids):
            flash('This combo is not available right now.', 'warning')
            return redirect(url_for('index'))
        cart = session.get('cart', {})
        for product in products:
            if product.stock < int(cart.get(str(product.id), 0)) + 1:
                flash(f'Not enough stock for {product.name} to add this combo.', 'warning')
                return redirect(url_for('cart'))
        for product in products:
            key = str(product.id)
            cart[key] = int(cart.get(key, 0)) + 1
        session['cart'] = cart
        session['combo_offer_id'] = combo.id
        save_customer_cart_snapshot()
        flash(f'{combo.title} added. The combo saving is included in your basket total.', 'success')
        return redirect(url_for('cart'))

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
        save_customer_cart_snapshot()
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
        save_customer_cart_snapshot()
        return redirect(url_for('cart'))

    @app.get('/cart/remove/<int:product_id>')
    def cart_remove(product_id):
        cart = session.get('cart', {})
        cart.pop(str(product_id), None)
        session['cart'] = cart
        save_customer_cart_snapshot()
        return redirect(url_for('cart'))

    def save_customer_cart_snapshot():
        if current_user.is_authenticated and current_user.role == 'customer':
            cart = session.get('cart', {})
            record = AbandonedCart.query.filter_by(user_id=current_user.id, recovered_at=None).first()
            if cart:
                if not record:
                    record = AbandonedCart(user_id=current_user.id)
                    db.session.add(record)
                record.cart_data = __import__('json').dumps(cart)
                record.updated_at = datetime.utcnow()
            elif record:
                db.session.delete(record)
            db.session.commit()

    def cart_data():
        cart = session.get('cart', {})
        rows, subtotal = [], Decimal('0.00')
        now_utc = datetime.utcnow()
        for pid, qty in cart.items():
            product = Product.query.get(int(pid))
            if product and product.active and product.stock > 0:
                quantity = min(max(1, int(qty)), product.stock)
                sale = FlashSale.query.filter(
                    FlashSale.product_id == product.id, FlashSale.active.is_(True),
                    FlashSale.starts_at <= now_utc, FlashSale.ends_at > now_utc,
                    FlashSale.sale_price > 0, FlashSale.sale_price < product.price
                ).order_by(FlashSale.sale_price.asc()).first()
                unit_price = Decimal(str(sale.sale_price if sale else product.price))
                line = unit_price * quantity
                subtotal += line
                rows.append({'product': product, 'quantity': quantity, 'unit_price': unit_price,
                             'flash_sale': sale, 'line_total': line})
        return rows, subtotal

    def active_cart_combo(rows):
        combo_id = session.get('combo_offer_id')
        if not combo_id:
            return None, Decimal('0.00')
        now_utc = datetime.utcnow()
        combo = ComboOffer.query.filter_by(id=combo_id, active=True).filter(
            db.or_(ComboOffer.starts_at.is_(None), ComboOffer.starts_at <= now_utc),
            db.or_(ComboOffer.ends_at.is_(None), ComboOffer.ends_at > now_utc)
        ).first()
        if not combo:
            session.pop('combo_offer_id', None)
            return None, Decimal('0.00')
        try:
            ids = [int(value) for value in __import__('json').loads(combo.product_ids)]
        except (ValueError, TypeError):
            session.pop('combo_offer_id', None)
            return None, Decimal('0.00')
        quantities = {row['product'].id: row['quantity'] for row in rows}
        row_map = {row['product'].id: row for row in rows}
        if len(ids) < 2 or any(pid not in quantities for pid in ids):
            session.pop('combo_offer_id', None)
            return None, Decimal('0.00')
        regular_bundle = sum((row_map[pid]['unit_price'] for pid in ids), Decimal('0.00'))
        combo_price = Decimal(str(combo.combo_price))
        discount = min(regular_bundle, max(Decimal('0.00'), regular_bundle - combo_price))
        return combo, discount.quantize(Decimal('0.01'))

    def current_vip_membership():
        if not current_user.is_authenticated or current_user.role != 'customer':
            return None
        return VipMembership.query.filter_by(user_id=current_user.id, active=True).filter(
            VipMembership.ends_at > datetime.utcnow()
        ).order_by(VipMembership.ends_at.desc()).first()

    def delivery_threshold_for_membership(membership=None):
        if membership and membership.tier == 'Platinum':
            return Decimal('0.00')
        if membership and membership.tier == 'Gold':
            return Decimal('150.00')
        return Decimal(str(app.config['FREE_DELIVERY_THRESHOLD']))

    def calculate_delivery_fee(subtotal, membership=None):
        threshold = delivery_threshold_for_membership(membership)
        if membership and membership.tier == 'Gold':
            return Decimal('0.00') if subtotal > threshold else Decimal(str(app.config['DELIVERY_FEE']))
        return Decimal('0.00') if subtotal >= threshold else Decimal(str(app.config['DELIVERY_FEE']))

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
        membership = current_vip_membership()
        threshold = delivery_threshold_for_membership(membership)
        fee = calculate_delivery_fee(subtotal, membership)
        coupon, coupon_discount = active_cart_coupon(subtotal)
        active_combo, combo_discount = active_cart_combo(rows)
        reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
        reward_credits += sum(item.points for item in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
        reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
        available_reward_points = max(0, reward_credits - int(reward_spent or 0))
        reward_discount = min(Decimal(available_reward_points), subtotal) if session.get('use_rewards') else Decimal('0.00')
        discount = min(subtotal, coupon_discount + reward_discount + combo_discount)
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
            'free_delivery_threshold': f'{delivery_threshold_for_membership(current_vip_membership()):.2f}',
            'discount': f'{discount:.2f}', 'coupon_discount': f'{coupon_discount:.2f}',
            'reward_discount': f'{reward_discount:.2f}', 'combo_discount': f'{combo_discount:.2f}',
            'combo_title': active_combo.title if active_combo else None, 'coupon_code': coupon.code if coupon else None,
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
        membership = current_vip_membership()
        fee = calculate_delivery_fee(subtotal, membership)
        coupon, coupon_discount = active_cart_coupon(subtotal)
        active_combo, combo_discount = active_cart_combo(rows)
        if current_user.is_authenticated and current_user.role == 'customer':
            reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
            reward_credits += sum(item.points for item in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
            reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
            available_reward_points = max(0, reward_credits - int(reward_spent or 0))
        else:
            available_reward_points = 0
        use_rewards = request.args.get('use_rewards') == '1'
        if request.args.get('use_rewards') in ['0', '1']:
            session['use_rewards'] = use_rewards
        else:
            use_rewards = bool(session.get('use_rewards'))
        reward_discount = min(Decimal(available_reward_points), subtotal) if use_rewards else Decimal('0.00')
        discount = min(subtotal, coupon_discount + reward_discount + combo_discount)
        total = max(Decimal('0.00'), subtotal + fee - discount)
        return render_template('cart.html', rows=rows, subtotal=subtotal, delivery_fee=fee,
                               total=total, threshold=threshold, coupon=coupon, discount=discount,
                               coupon_discount=coupon_discount, reward_discount=reward_discount,
                               combo=active_combo, combo_discount=combo_discount, membership=membership)

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
        membership = current_vip_membership()
        fee = calculate_delivery_fee(subtotal, membership)
        coupon, coupon_discount = active_cart_coupon(subtotal)
        active_combo, combo_discount = active_cart_combo(rows)
        reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
        reward_credits += sum(item.points for item in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
        reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
        available_reward_points = max(0, reward_credits - int(reward_spent or 0))
        use_rewards = request.args.get('use_rewards') == '1'
        if request.args.get('use_rewards') in ['0', '1']:
            session['use_rewards'] = use_rewards
        else:
            use_rewards = bool(session.get('use_rewards'))
        reward_discount = min(Decimal(available_reward_points), subtotal) if use_rewards else Decimal('0.00')
        discount = min(subtotal, coupon_discount + reward_discount + combo_discount)
        total = max(Decimal('0.00'), subtotal + fee - discount)
        wallet_opted = request.values.get('use_wallet') == '1'
        wallet_available = wallet_balance(current_user.id)
        wallet_preview = min(wallet_available, total) if wallet_opted else Decimal('0.00')
        total = max(Decimal('0.00'), total - wallet_preview)
        local_today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        delivery_date = request.values.get('delivery_date') or local_today.strftime('%Y-%m-%d')
        delivery_slots = available_delivery_slots(delivery_date)
        delivery_slot = request.values.get('delivery_slot', '')

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
            elif delivery_slot not in available_delivery_slots(delivery_date):
                flash('Please choose a valid available delivery date and time slot.', 'warning')
            elif subtotal < app.config['MIN_ORDER_VALUE']:
                flash(f"Minimum order value is {app.config['CURRENCY']}{app.config['MIN_ORDER_VALUE']}.", 'warning')
            elif payment == 'UPI' and total > 0 and not upi_reference:
                flash('After paying by UPI, enter the UPI transaction reference / UTR.', 'warning')
            else:
                # Re-check stock and calculate all prices on the server.
                for row in rows:
                    if row['product'].stock < row['quantity']:
                        flash(f"Insufficient stock for {row['product'].name}.", 'danger')
                        return redirect(url_for('cart'))
                # Revalidate coupons and reward balance on the server; never trust browser totals.
                coupon, coupon_discount = active_cart_coupon(subtotal)
                active_combo, combo_discount = active_cart_combo(rows)
                reward_credits = sum(item.points for item in RewardTransaction.query.filter_by(user_id=current_user.id).all())
                reward_credits += sum(item.points for item in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
                reward_spent = db.session.query(db.func.coalesce(db.func.sum(RewardRedemption.points), 0)).filter_by(user_id=current_user.id).scalar()
                available_reward_points = max(0, reward_credits - int(reward_spent or 0))
                reward_discount = min(Decimal(available_reward_points), subtotal) if session.get('use_rewards') else Decimal('0.00')
                discount = min(subtotal, coupon_discount + reward_discount + combo_discount)
                total = max(Decimal('0.00'), subtotal + fee - discount)
                wallet_used = min(wallet_balance(current_user.id), total) if request.form.get('use_wallet') == '1' else Decimal('0.00')
                total = max(Decimal('0.00'), total - wallet_used)
                if payment == 'UPI' and total == Decimal('0.00'):
                    payment = 'COD'
                order = Order(order_number=f"TB{datetime.utcnow():%y%m%d%H%M%S}{current_user.id}", user_id=current_user.id,
                              customer_name=name, phone=phone, address_text=address, subtotal=subtotal,
                              discount=discount, delivery_fee=fee, total=total, payment_method=payment,
                              payment_status='Pending' if payment == 'COD' else 'Awaiting UPI verification',
                              status='Pending',
                              notes=(f'UPI reference submitted by customer: {upi_reference}' if payment == 'UPI' else ''))
                db.session.add(order)
                db.session.flush()
                db.session.add(DeliverySchedule(order_id=order.id, delivery_date=delivery_date, delivery_slot=delivery_slot))
                create_customer_notification(order, 'Order placed successfully', f'Your order {order.order_number} has been received. Requested delivery: {delivery_date} at {delivery_slot}. We will confirm it shortly.')
                for row in rows:
                    p = row['product']
                    db.session.add(OrderItem(order_id=order.id, product_id=p.id, product_name=p.name,
                                             unit=p.unit, quantity=row['quantity'], unit_price=row['unit_price']))
                    p.stock -= row['quantity']
                    # Consume tracked stock batches in earliest-expiry-first order.
                    remaining_to_allocate = int(row['quantity'])
                    tracked_batches = StockBatch.query.filter(
                        StockBatch.product_id == p.id, StockBatch.remaining_quantity > 0
                    ).order_by(StockBatch.expiry_date.asc(), StockBatch.id.asc()).all()
                    for batch in tracked_batches:
                        if remaining_to_allocate <= 0:
                            break
                        used = min(int(batch.remaining_quantity), remaining_to_allocate)
                        batch.remaining_quantity -= used
                        remaining_to_allocate -= used
                db.session.flush()
                if reward_discount > 0:
                    points_used = int(reward_discount)
                    db.session.add(RewardRedemption(user_id=current_user.id, order_id=order.id, points=points_used))
                if wallet_used > 0:
                    db.session.add(WalletTransaction(user_id=current_user.id, amount=wallet_used, kind='Debit',
                                                     reference=order.order_number, description='Used for order payment'))
                db.session.commit()
                pending_subscription_id = session.pop('pending_subscription_id', None)
                if pending_subscription_id:
                    completed_subscription = GrocerySubscription.query.filter_by(id=pending_subscription_id, user_id=current_user.id, active=True).first()
                    if completed_subscription:
                        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
                        delta = {'Weekly': 7, 'Fortnightly': 14, 'Monthly': 30}.get(completed_subscription.frequency, 7)
                        try:
                            due = datetime.strptime(completed_subscription.next_date, '%Y-%m-%d').date()
                        except ValueError:
                            due = today
                        while due <= today:
                            due += timedelta(days=delta)
                        completed_subscription.next_date = due.isoformat()
                        db.session.commit()
                session['cart'] = {}
                session.pop('combo_offer_id', None)
                abandoned_record = AbandonedCart.query.filter_by(user_id=current_user.id, recovered_at=None).first()
                if abandoned_record:
                    abandoned_record.recovered_at = datetime.utcnow()
                session.pop('coupon_code', None)
                session.pop('use_rewards', None)
                flash(f'Order {order.order_number} placed successfully. UPI payment will be verified by our team.' if payment == 'UPI' else f'Order {order.order_number} placed successfully. Our team will confirm it shortly.', 'success')
                return redirect(url_for('my_orders'))
        saved_addresses = Address.query.filter_by(user_id=current_user.id).order_by(Address.id.desc()).all()
        active_combo, combo_discount = active_cart_combo(rows)
        return render_template('checkout.html', rows=rows, subtotal=subtotal, delivery_fee=fee, total=total,
                               coupon=coupon, discount=discount, reward_discount=reward_discount,
                               membership=membership, combo=active_combo, combo_discount=combo_discount,
                               available_reward_points=available_reward_points, use_rewards=use_rewards,
                               wallet_balance=wallet_available, wallet_opted=wallet_opted, wallet_used=wallet_preview,
                               saved_addresses=saved_addresses, upi_qr_data=upi_qr_data, upi_id='8838145515@sbi',
                               delivery_date=delivery_date, delivery_slots=delivery_slots, delivery_slot=delivery_slot,
                               min_delivery_date=local_today.strftime('%Y-%m-%d'),
                               max_delivery_date=(local_today + timedelta(days=7)).strftime('%Y-%m-%d'))
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

    @app.post('/order/<int:order_id>/add-item')
    @login_required
    def order_add_item(order_id):
        if current_user.role != 'customer':
            abort(403)
        order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
        if order.status not in ['Pending', 'Confirmed'] or order.delivery_partner_id is not None or order.payment_method != 'COD' or order.payment_status != 'Pending':
            flash('Items can only be added to an early-stage COD order before store processing begins.', 'warning')
            return redirect(url_for('order_detail', order_id=order.id))
        product = Product.query.filter_by(id=request.form.get('product_id', type=int), active=True).first_or_404()
        quantity = max(1, min(10, request.form.get('quantity', 1, type=int) or 1))
        if product.stock < quantity:
            flash(f'Only {product.stock} unit(s) of {product.name} are currently available.', 'warning')
            return redirect(url_for('order_detail', order_id=order.id))
        existing = next((line for line in order.items if line.product_id == product.id), None)
        if existing:
            existing.quantity += quantity
        else:
            db.session.add(OrderItem(order_id=order.id, product_id=product.id, product_name=product.name, unit=product.unit, quantity=quantity, unit_price=product.price))
        product.stock -= quantity
        remaining_to_allocate = quantity
        batches = StockBatch.query.filter(StockBatch.product_id == product.id, StockBatch.remaining_quantity > 0).order_by(StockBatch.expiry_date.asc(), StockBatch.id.asc()).all()
        for batch in batches:
            if remaining_to_allocate <= 0: break
            used = min(int(batch.remaining_quantity), remaining_to_allocate)
            batch.remaining_quantity -= used
            remaining_to_allocate -= used
        order.subtotal = Decimal(str(order.subtotal or 0)) + Decimal(str(product.price)) * quantity
        order.delivery_fee = calculate_delivery_fee(order.subtotal, current_vip_membership())
        order.total = max(Decimal('0.00'), Decimal(str(order.subtotal)) + Decimal(str(order.delivery_fee or 0)) - Decimal(str(order.discount or 0)))
        create_customer_notification(order, 'Items added to your order', f'{quantity} × {product.name} was added. Your updated order total is ₹{order.total:.2f}.')
        db.session.commit()
        flash(f'{quantity} × {product.name} added. Updated total: ₹{order.total:.2f}.', 'success')
        return redirect(url_for('order_detail', order_id=order.id))
    @app.post('/order/<int:order_id>/cancel')
    @login_required
    def cancel_order(order_id):
        if current_user.role != 'customer':
            abort(403)
        order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
        if order.delivery_partner_id is not None or order.status in ['Delivered', 'Cancelled']:
            flash('This order can only be cancelled before a delivery partner is assigned. Please contact the store team for help.', 'warning')
            return redirect(url_for('order_detail', order_id=order.id))
        for item in order.items:
            if item.product_id:
                product = Product.query.get(item.product_id)
                if product:
                    product.stock = int(product.stock or 0) + int(item.quantity or 0)
                    restore_batch = StockBatch.query.filter(
                        StockBatch.product_id == product.id,
                        StockBatch.expiry_date >= datetime.now(ZoneInfo('Asia/Kolkata')).date().strftime('%Y-%m-%d')
                    ).order_by(StockBatch.expiry_date.asc(), StockBatch.id.asc()).first()
                    if restore_batch:
                        restore_batch.remaining_quantity += int(item.quantity or 0)
        order.status = 'Cancelled'
        create_customer_notification(order, 'Order cancelled', 'Your order has been cancelled successfully.')
        db.session.commit()
        flash(f'Order {order.order_number} was cancelled. Item quantities have been returned to stock.', 'success')
        return redirect(url_for('order_detail', order_id=order.id))

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
        addable_products = []
        if current_user.role == 'customer' and order.user_id == current_user.id and order.status in ['Pending', 'Confirmed'] and order.delivery_partner_id is None and order.payment_method == 'COD' and order.payment_status == 'Pending':
            addable_products = Product.query.filter(Product.active.is_(True), Product.stock > 0).order_by(Product.name.asc()).limit(150).all()
        return render_template('order_detail.html', order=order, addable_products=addable_products)

    @app.get('/order/<int:order_id>/invoice')
    @login_required
    def order_invoice(order_id):
        from io import BytesIO
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

        order = Order.query.get_or_404(order_id)
        if current_user.role != 'admin' and order.user_id != current_user.id:
            abort(403)

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                                topMargin=18*mm, bottomMargin=18*mm)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name='StoreTitle', parent=styles['Title'], textColor=colors.HexColor('#176b39'),
                                  fontName='Helvetica-Bold', fontSize=22, spaceAfter=4))
        styles.add(ParagraphStyle(name='RightSmall', parent=styles['Normal'], alignment=TA_RIGHT, fontSize=9))
        story = [
            Paragraph('THARAI BASKET', styles['StoreTitle']),
            Paragraph('Everyday essentials, closer to home · Tharamangalam, Salem District, Tamil Nadu, India',
                      styles['Normal']),
            Spacer(1, 8*mm),
            Paragraph('TAX INVOICE / ORDER RECEIPT', styles['Heading2']),
        ]
        details = [
            ['Order number', order.order_number, 'Order date', order.created_at.strftime('%d %b %Y, %I:%M %p')],
            ['Customer', order.customer_name, 'Phone', order.phone or '—'],
            ['Delivery address', Paragraph((order.address_text or '—').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\n','<br/>'), styles['Normal']), 'Status', order.status],
            ['Payment method', order.payment_method or '—', 'Payment status', order.payment_status or '—'],
        ]
        info = Table(details, colWidths=[28*mm, 62*mm, 28*mm, 55*mm])
        info.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(0,-1),colors.HexColor('#f0f6f1')),
            ('BACKGROUND',(2,0),(2,-1),colors.HexColor('#f0f6f1')),
            ('TEXTCOLOR',(0,0),(0,-1),colors.HexColor('#42634a')),
            ('TEXTCOLOR',(2,0),(2,-1),colors.HexColor('#42634a')),
            ('FONTNAME',(0,0),(0,-1),'Helvetica-Bold'),('FONTNAME',(2,0),(2,-1),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),8),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#dce7dd')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),
            ('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ]))
        story.extend([info, Spacer(1, 8*mm)])
        rows = [['Item', 'Unit', 'Qty', 'Unit price', 'Amount']]
        for item in order.items:
            rows.append([item.product_name, item.unit or '—', str(item.quantity),
                         f'INR {Decimal(str(item.unit_price or 0)):.2f}',
                         f'INR {Decimal(str(item.unit_price or 0))*int(item.quantity or 0):.2f}'])
        items_table = Table(rows, colWidths=[67*mm, 28*mm, 14*mm, 35*mm, 35*mm], repeatRows=1)
        items_table.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#176b39')),('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),8),
            ('GRID',(0,0),(-1,-1),.35,colors.HexColor('#dce7dd')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('ALIGN',(2,1),(-1,-1),'RIGHT'),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f8fbf8')]),
            ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
        ]))
        story.extend([items_table, Spacer(1, 6*mm)])
        summary = [
            ['Subtotal', f'INR {Decimal(str(order.subtotal or 0)):.2f}'],
            ['Discount', f'- INR {Decimal(str(order.discount or 0)):.2f}'],
            ['Delivery fee', f'INR {Decimal(str(order.delivery_fee or 0)):.2f}'],
            ['Grand total', f'INR {Decimal(str(order.total or 0)):.2f}'],
        ]
        totals = Table(summary, colWidths=[145*mm, 34*mm], hAlign='RIGHT')
        totals.setStyle(TableStyle([
            ('ALIGN',(1,0),(1,-1),'RIGHT'),('FONTNAME',(0,0),(-1,-2),'Helvetica'),
            ('FONTNAME',(0,-1),(-1,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),9),
            ('TEXTCOLOR',(0,-1),(-1,-1),colors.HexColor('#176b39')),
            ('LINEABOVE',(0,-1),(-1,-1),1,colors.HexColor('#176b39')),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ]))
        story.extend([totals, Spacer(1, 12*mm),
                      Paragraph('Thank you for shopping with Tharai Basket!', styles['Heading3']),
                      Paragraph('This is a computer-generated invoice / order receipt.', styles['RightSmall'])])
        doc.build(story)
        buffer.seek(0)
        return app.response_class(buffer.getvalue(), mimetype='application/pdf',
                                  headers={'Content-Disposition': f'attachment; filename=Tharai-Basket-{order.order_number}.pdf'})

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
        low_stock_products = Product.query.filter(
            Product.active.is_(True), Product.stock <= 5
        ).order_by(Product.stock.asc(), Product.name.asc()).limit(6).all()
        recent_reviews = ProductReview.query.order_by(ProductReview.created_at.desc()).limit(8).all()
        review_count = ProductReview.query.count()
        average_review_rating = db.session.query(db.func.avg(ProductReview.rating)).scalar() or 0
        wallet_cashback_total = db.session.query(db.func.coalesce(db.func.sum(WalletTransaction.amount), 0)).filter(
            WalletTransaction.kind == 'Credit',
            WalletTransaction.reference.like('ORDER-CASHBACK-%')
        ).scalar() or 0
        delivered_items = OrderItem.query.join(Order).filter(Order.status == 'Delivered').all()
        gross_profit = sum(((Decimal(str(item.unit_price or 0)) - Decimal(str(item.purchase_cost or 0))) * int(item.quantity or 0)
                            for item in delivered_items), Decimal('0.00'))
        recorded_expenses = db.session.query(db.func.coalesce(db.func.sum(StoreExpense.amount), 0)).scalar() or 0
        estimated_net = gross_profit - Decimal(str(recorded_expenses or 0))
        return render_template('admin/dashboard.html',
            order_count=Order.query.count(), customer_count=User.query.filter_by(role='customer').count(),
            product_count=Product.query.filter_by(active=True).count(),
            pending_count=Order.query.filter(Order.status.in_(['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Out for Delivery'])).count(),
            total_sales=total_sales, monthly_revenue=monthly_revenue, today_revenue=today_revenue,
            delivered_count=delivered_query.count(), low_stock=Product.query.filter(Product.stock <= 5, Product.active.is_(True)).count(),
            daily_sales=daily_sales, max_daily_sales=max_daily_sales, top_products=top_products,
            status_breakdown=status_breakdown, payment_breakdown=payment_breakdown, orders=recent_orders,
            low_stock_products=low_stock_products, recent_reviews=recent_reviews,
            review_count=review_count, average_review_rating=round(float(average_review_rating), 1),
            wallet_cashback_total=wallet_cashback_total, gross_profit=gross_profit,
            recorded_expenses=recorded_expenses, estimated_net=estimated_net)

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
        today_key = datetime.now(ZoneInfo('Asia/Kolkata')).date().strftime('%Y-%m-%d')
        expiry_cutoff = (datetime.now(ZoneInfo('Asia/Kolkata')).date() + timedelta(days=30)).strftime('%Y-%m-%d')
        expiry_batches = StockBatch.query.filter(StockBatch.remaining_quantity > 0,
            StockBatch.expiry_date <= expiry_cutoff).order_by(StockBatch.expiry_date.asc()).all()
        expired_batch_count = sum(1 for batch in expiry_batches if batch.expiry_date < today_key)
        return render_template('admin/inventory.html', products=products,
            out_of_stock_count=out_of_stock_count, low_stock_count=low_stock_count,
            all_products=Product.query.filter_by(active=True).order_by(Product.name).all(),
            expiry_batches=expiry_batches, expired_batch_count=expired_batch_count, today_key=today_key)

    @app.post('/admin/inventory/batches')
    @admin_required
    def admin_inventory_add_batch():
        product = Product.query.filter_by(id=request.form.get('product_id', type=int), active=True).first_or_404()
        try:
            quantity = int(request.form.get('quantity', ''))
            expiry_date = request.form.get('expiry_date', '')
            datetime.strptime(expiry_date, '%Y-%m-%d')
        except (TypeError, ValueError):
            flash('Enter a valid quantity and expiry date.', 'danger')
            return redirect(url_for('admin_inventory'))
        if quantity < 1 or quantity > 1000000:
            flash('Batch quantity must be between 1 and 1,000,000.', 'danger')
            return redirect(url_for('admin_inventory'))
        db.session.add(StockBatch(product_id=product.id, received_quantity=quantity,
            remaining_quantity=quantity, expiry_date=expiry_date))
        product.stock = int(product.stock or 0) + quantity
        db.session.commit()
        flash(f'{quantity} units added to {product.name}; expiry recorded for {expiry_date}.', 'success')
        return redirect(url_for('admin_inventory'))

    @app.post('/admin/inventory/batches/<int:batch_id>/remove-expired')
    @admin_required
    def admin_inventory_remove_expired(batch_id):
        batch = StockBatch.query.get_or_404(batch_id)
        if batch.expiry_date >= datetime.now(ZoneInfo('Asia/Kolkata')).date().strftime('%Y-%m-%d'):
            flash('This batch has not expired yet. Only expired batches can be removed here.', 'warning')
            return redirect(url_for('admin_inventory'))
        product = Product.query.get_or_404(batch.product_id)
        remove_qty = min(int(batch.remaining_quantity or 0), int(product.stock or 0))
        product.stock = max(0, int(product.stock or 0) - remove_qty)
        batch.remaining_quantity = 0
        db.session.commit()
        flash(f'Expired batch removed: {remove_qty} units of {product.name}.', 'success')
        return redirect(url_for('admin_inventory'))

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

    @app.route('/admin/delivery-slots', methods=['GET', 'POST'])
    @admin_required
    def admin_delivery_slot_settings():
        setting = DeliverySlotSetting.query.first()
        if not setting:
            setting = DeliverySlotSetting(capacity=5)
            db.session.add(setting)
            db.session.commit()
        if request.method == 'POST':
            capacity = request.form.get('capacity', type=int)
            if capacity is None or capacity < 1 or capacity > 100:
                flash('Slot capacity must be between 1 and 100 orders.', 'danger')
            else:
                setting.capacity = capacity
                db.session.commit()
                flash('Delivery slot capacity updated.', 'success')
            return redirect(url_for('admin_delivery_slot_settings'))
        upcoming = DeliverySchedule.query.join(Order).filter(
            DeliverySchedule.delivery_date >= datetime.now(ZoneInfo('Asia/Kolkata')).date().strftime('%Y-%m-%d'),
            Order.status != 'Cancelled'
        ).order_by(DeliverySchedule.delivery_date, DeliverySchedule.delivery_slot).all()
        return render_template('admin/delivery_slots.html', setting=setting, schedules=upcoming)

    @app.get('/admin/reports/export.csv')
    @admin_required
    def admin_reports_export():
        import csv
        from io import StringIO
        period = request.args.get('period', 'month')
        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        if period == 'today':
            start_date = today
        elif period == '7days':
            start_date = today - timedelta(days=6)
        elif period == 'all':
            start_date = None
        else:
            period = 'month'
            start_date = today.replace(day=1)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Type', 'Date', 'Reference', 'Customer / Description', 'Items / Category', 'Status', 'Amount INR'])
        order_query = Order.query.filter_by(status='Delivered')
        if start_date:
            order_query = order_query.filter(Order.created_at >= datetime.combine(start_date, datetime.min.time()))
        for order in order_query.order_by(Order.created_at.asc()).all():
            item_summary = '; '.join(f'{item.product_name} x {item.quantity}' for item in order.items)
            writer.writerow(['Sale', order.created_at.strftime('%Y-%m-%d %H:%M'), order.order_number,
                order.customer_name, item_summary, order.status, f'{Decimal(str(order.total or 0)):.2f}'])
        expense_query = StoreExpense.query
        if start_date:
            expense_query = expense_query.filter(StoreExpense.expense_date >= start_date.strftime('%Y-%m-%d'))
        for expense in expense_query.order_by(StoreExpense.expense_date.asc()).all():
            writer.writerow(['Expense', expense.expense_date, f'EXP-{expense.id}', expense.title,
                expense.category, 'Recorded', f'-{Decimal(str(expense.amount or 0)):.2f}'])
        return app.response_class('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename=tharai-basket-report-{period}.csv'})

    @app.route('/admin/expenses', methods=['GET', 'POST'])
    @admin_required
    def admin_expenses():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            category = request.form.get('category', 'Other').strip() or 'Other'
            date_value = request.form.get('expense_date', '')
            amount = money(request.form.get('amount'))
            try:
                datetime.strptime(date_value, '%Y-%m-%d')
            except (TypeError, ValueError):
                date_value = ''
            if not title or not date_value or amount <= 0:
                flash('Enter a title, valid date and amount greater than zero.', 'danger')
            else:
                db.session.add(StoreExpense(title=title, category=category, amount=amount,
                                            expense_date=date_value, notes=request.form.get('notes', '').strip()))
                db.session.commit()
                flash('Expense recorded.', 'success')
            return redirect(url_for('admin_expenses'))
        expenses = StoreExpense.query.order_by(StoreExpense.expense_date.desc(), StoreExpense.id.desc()).limit(200).all()
        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        month_start = today.replace(day=1).strftime('%Y-%m-%d')
        month_expenses = db.session.query(db.func.coalesce(db.func.sum(StoreExpense.amount), 0)).filter(StoreExpense.expense_date >= month_start).scalar()
        month_revenue = db.session.query(db.func.coalesce(db.func.sum(Order.total), 0)).filter(
            Order.status == 'Delivered', Order.created_at >= datetime.combine(today.replace(day=1), datetime.min.time())).scalar()
        return render_template('admin/expenses.html', expenses=expenses, month_expenses=month_expenses,
                               month_revenue=month_revenue, month_profit=Decimal(str(month_revenue or 0))-Decimal(str(month_expenses or 0)),
                               today=today.strftime('%Y-%m-%d'))

    @app.get('/admin/abandoned-carts')
    @admin_required
    def admin_abandoned_carts():
        carts = AbandonedCart.query.filter_by(recovered_at=None).order_by(AbandonedCart.updated_at.desc()).limit(200).all()
        return render_template('admin/abandoned_carts.html', carts=carts)

    def membership_plan_amount(tier, months):
        prices = {
            ('Gold', 1): Decimal('300.00'),
            ('Gold', 3): Decimal('800.00'),
            ('Platinum', 1): Decimal('500.00'),
            ('Platinum', 3): Decimal('1300.00'),
        }
        return prices.get((tier, months))

    def membership_qr_data(amount, tier, months):
        params = {'pa': '8838145515@sbi', 'pn': 'DHARUN M',
                  'am': f'{amount:.2f}', 'cu': 'INR',
                  'tn': f'Tharai Basket {tier} {months} month membership'}
        qr = qrcode.make('upi://pay?' + urlencode(params))
        buffer = io.BytesIO()
        qr.save(buffer, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')

    @app.get('/account/membership')
    @login_required
    def account_membership():
        if current_user.role != 'customer': abort(403)
        membership = VipMembership.query.filter_by(user_id=current_user.id, active=True).filter(
            VipMembership.ends_at > datetime.utcnow()).order_by(VipMembership.ends_at.desc()).first()
        pending_request = MembershipPayment.query.filter_by(user_id=current_user.id, status='Pending').order_by(
            MembershipPayment.created_at.desc()).first()
        plans = {
            'Gold': {1: {'amount': Decimal('300.00'), 'qr': membership_qr_data(Decimal('300.00'), 'Gold', 1)},
                     3: {'amount': Decimal('800.00'), 'qr': membership_qr_data(Decimal('800.00'), 'Gold', 3)}},
            'Platinum': {1: {'amount': Decimal('500.00'), 'qr': membership_qr_data(Decimal('500.00'), 'Platinum', 1)},
                         3: {'amount': Decimal('1300.00'), 'qr': membership_qr_data(Decimal('1300.00'), 'Platinum', 3)}},
        }
        upgrade_amount = None
        upgrade_qr = None
        if membership and membership.tier == 'Gold':
            total_seconds = max((membership.ends_at - membership.starts_at).total_seconds(), 1)
            remaining_seconds = max((membership.ends_at - datetime.utcnow()).total_seconds(), 0)
            base_gold = Decimal('800.00') if total_seconds > 45 * 86400 else Decimal('300.00')
            base_platinum = Decimal('1300.00') if total_seconds > 45 * 86400 else Decimal('500.00')
            upgrade_amount = ((base_platinum - base_gold) * Decimal(str(remaining_seconds / total_seconds))).quantize(Decimal('0.01'))
            upgrade_qr = membership_qr_data(upgrade_amount, 'Platinum Upgrade', 0) if upgrade_amount > 0 else None
        return render_template('membership.html', membership=membership, pending_request=pending_request,
                               plans=plans, upi_id='8838145515@sbi',
                               upgrade_amount=upgrade_amount, upgrade_qr=upgrade_qr)

    @app.post('/account/membership/request')
    @login_required
    def membership_request():
        if current_user.role != 'customer': abort(403)
        tier = request.form.get('tier', '').title()
        months = request.form.get('months', type=int)
        upi_reference = request.form.get('upi_reference', '').strip()
        amount = membership_plan_amount(tier, months)
        active_membership = current_vip_membership()
        if amount is None:
            flash('Choose a valid Gold or Platinum membership plan.', 'danger')
        elif active_membership:
            flash('You already have an active membership. Use the upgrade option if you are a Gold member.', 'info')
        elif not upi_reference or len(upi_reference) < 6 or len(upi_reference) > 100:
            flash('Enter the UPI transaction reference / UTR after payment.', 'warning')
        elif MembershipPayment.query.filter_by(user_id=current_user.id, status='Pending').first():
            flash('You already have a membership payment waiting for verification.', 'info')
        else:
            db.session.add(MembershipPayment(user_id=current_user.id, tier=tier, months=months,
                                             amount=amount, upi_reference=upi_reference[:100]))
            db.session.commit()
            flash('Membership payment details submitted. Your plan will activate after store verification.', 'success')
        return redirect(url_for('account_membership'))

    @app.post('/account/membership/upgrade')
    @login_required
    def membership_upgrade():
        if current_user.role != 'customer': abort(403)
        membership = current_vip_membership()
        upi_reference = request.form.get('upi_reference', '').strip()
        pending = MembershipPayment.query.filter_by(user_id=current_user.id, status='Pending').first()
        if not membership or membership.tier != 'Gold':
            flash('Only active Gold members can upgrade to Platinum.', 'danger')
        elif pending:
            flash('You already have a membership payment waiting for verification.', 'info')
        elif not upi_reference or len(upi_reference) < 6 or len(upi_reference) > 100:
            flash('Enter the UPI transaction reference / UTR after paying.', 'warning')
        else:
            total_seconds = max((membership.ends_at - membership.starts_at).total_seconds(), 1)
            remaining_seconds = max((membership.ends_at - datetime.utcnow()).total_seconds(), 0)
            base_gold = Decimal('800.00') if total_seconds > 45 * 86400 else Decimal('300.00')
            base_platinum = Decimal('1300.00') if total_seconds > 45 * 86400 else Decimal('500.00')
            upgrade_amount = ((base_platinum - base_gold) * Decimal(str(remaining_seconds / total_seconds))).quantize(Decimal('0.01'))
            if upgrade_amount <= 0:
                flash('Your Gold membership has expired. Please choose a new Platinum plan.', 'warning')
            else:
                db.session.add(MembershipPayment(user_id=current_user.id, tier='Platinum', months=0,
                    amount=upgrade_amount, upi_reference=upi_reference[:100]))
                db.session.commit()
                flash(f'Platinum upgrade request submitted for ₹{upgrade_amount:.2f}. It will activate after verification.', 'success')
        return redirect(url_for('account_membership'))

    @app.post('/admin/membership-requests/<int:payment_id>/approve')
    @admin_required
    def admin_membership_request_approve(payment_id):
        payment = MembershipPayment.query.filter_by(id=payment_id, status='Pending').first_or_404()
        existing_membership = VipMembership.query.filter_by(user_id=payment.user_id, active=True).filter(
            VipMembership.ends_at > datetime.utcnow()).order_by(VipMembership.ends_at.desc()).first()
        if payment.months == 0 and payment.tier == 'Platinum':
            if not existing_membership or existing_membership.tier != 'Gold':
                flash('This Gold membership is no longer active. Reject this upgrade request; ask the customer to purchase a full Platinum plan.', 'warning')
                return redirect(url_for('admin_growth_tools'))
            existing_membership.tier = 'Platinum'
        else:
            VipMembership.query.filter_by(user_id=payment.user_id, active=True).update({'active': False})
            db.session.add(VipMembership(user_id=payment.user_id, tier=payment.tier,
                starts_at=datetime.utcnow(), ends_at=datetime.utcnow() + timedelta(days=30 * payment.months), active=True))
        payment.status = 'Approved'
        payment.reviewed_at = datetime.utcnow()
        payment.reviewed_by = current_user.id
        db.session.commit()
        flash(f'{payment.tier} membership activated for {payment.user.name}.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/membership-requests/<int:payment_id>/reject')
    @admin_required
    def admin_membership_request_reject(payment_id):
        payment = MembershipPayment.query.filter_by(id=payment_id, status='Pending').first_or_404()
        payment.status = 'Rejected'
        payment.reviewed_at = datetime.utcnow()
        payment.reviewed_by = current_user.id
        db.session.commit()
        flash('Membership payment request rejected.', 'info')
        return redirect(url_for('admin_growth_tools'))

    @app.get('/admin/dynamic-pricing')
    @admin_required
    def admin_dynamic_pricing():
        suggestions = []
        for product in Product.query.filter_by(active=True).all():
            sold = db.session.query(db.func.coalesce(db.func.sum(OrderItem.quantity),0)).join(
                Order, Order.id == OrderItem.order_id).filter(OrderItem.product_id == product.id,
                Order.status == 'Delivered', Order.created_at >= datetime.utcnow()-timedelta(days=30)).scalar() or 0
            if product.stock <= 3: action, reason = 'Protect margin', 'Very low stock; avoid discounting and replenish.'
            elif product.stock >= 25 and sold == 0: action, reason = 'Review a promotion', 'High stock and no delivered sales in 30 days.'
            elif product.stock >= 15 and sold < 3: action, reason = 'Consider a combo', 'Slow movement with elevated stock.'
            else: action, reason = 'Keep current price', 'Stock and recent sales look balanced.'
            suggestions.append({'product':product,'sold':int(sold),'action':action,'reason':reason})
        return render_template('admin/dynamic_pricing.html', suggestions=suggestions)

    @app.route('/admin/notifications/broadcast', methods=['GET', 'POST'])
    @admin_required
    def admin_notification_broadcast():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()[:140]
            message = request.form.get('message', '').strip()[:500]
            audience = request.form.get('audience', 'all')
            if not title or not message or audience not in ('all', 'recent'):
                flash('Enter a title, message and valid audience.', 'warning')
                return redirect(url_for('admin_notification_broadcast'))
            customers = User.query.filter_by(role='customer', active=True).all()
            if audience == 'recent':
                cutoff = datetime.utcnow() - timedelta(days=30)
                recent_ids = {row[0] for row in db.session.query(Order.user_id).filter(Order.created_at >= cutoff).distinct().all()}
                customers = [customer for customer in customers if customer.id in recent_ids]
            db.session.add_all([Notification(user_id=customer.id, title=title, message=message,
                category='announcement') for customer in customers])
            db.session.commit()
            flash(f'Announcement sent to {len(customers)} customer account(s).', 'success')
            return redirect(url_for('admin_notification_broadcast'))
        return render_template('admin/notifications_broadcast.html')

    @app.get('/admin/inventory/reorder-suggestions')
    @admin_required
    def admin_reorder_suggestions():
        low_products = Product.query.filter(Product.active.is_(True), Product.stock <= 10).order_by(Product.stock.asc()).all()
        suggestions = []
        for product in low_products:
            matches = SupplierProduct.query.filter(
                db.func.lower(SupplierProduct.product_name) == product.name.lower(),
                SupplierProduct.available.is_(True)).all()
            for item in matches:
                suggestions.append({'product': product, 'supplier_item': item,
                    'suggested_qty': max(int(item.min_qty or 1), 10 - int(product.stock or 0))})
        return render_template('admin/reorder_suggestions.html', suggestions=suggestions, low_products=low_products)

    @app.get('/admin/growth-tools')
    @admin_required
    def admin_growth_tools():
        return render_template('admin/growth_tools.html',
            products=Product.query.filter_by(active=True).order_by(Product.name).all(),
            combos=ComboOffer.query.order_by(ComboOffer.created_at.desc()).all(),
            sales=FlashSale.query.order_by(FlashSale.ends_at.desc()).all(),
            customers=User.query.filter_by(role='customer', active=True).order_by(User.name).all(),
            membership_requests=MembershipPayment.query.order_by(MembershipPayment.created_at.desc()).limit(100).all(),
            active_memberships=VipMembership.query.filter_by(active=True).order_by(VipMembership.starts_at.desc()).all())

    @app.post('/admin/growth-tools/gallery')
    @admin_required
    def admin_gallery_add():
        product = Product.query.get_or_404(request.form.get('product_id', type=int))
        image_url = request.form.get('image_url', '').strip()
        image = request.files.get('image')
        if image_url:
            if not image_url.startswith(('https://', 'http://')):
                flash('Enter a valid https:// image URL.', 'danger'); return redirect(url_for('admin_growth_tools'))
            stored_path = image_url
        elif image and image.filename:
            filename = secure_filename(image.filename)
            if Path(filename).suffix.lower() not in {'.jpg','.jpeg','.png','.webp'}:
                flash('Use JPG, PNG or WEBP.', 'danger'); return redirect(url_for('admin_growth_tools'))
            safe = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{filename}"
            image.save(Path(app.config['UPLOAD_FOLDER']) / safe)
            stored_path = f'uploads/{safe}'
        else:
            flash('Provide an image URL or upload a JPG, PNG or WEBP image.', 'warning')
            return redirect(url_for('admin_growth_tools'))
        db.session.add(ProductGalleryImage(product_id=product.id, image_path=stored_path,
                                            alt_text=(request.form.get('alt_text') or product.name)[:180]))
        db.session.commit()
        flash('Gallery image added. Note: local Render uploads may be lost on redeploy; durable image hosting is not yet configured.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/growth-tools/combo')
    @admin_required
    def admin_combo_add():
        ids = list(dict.fromkeys(x for x in request.form.getlist('product_ids') if x.isdigit()))
        price = money(request.form.get('combo_price'))
        if len(ids) < 2 or price <= 0 or not request.form.get('title','').strip():
            flash('Enter a title, at least two products and a valid price.', 'danger'); return redirect(url_for('admin_growth_tools'))
        valid = Product.query.filter(Product.id.in_([int(x) for x in ids]), Product.active.is_(True)).all()
        if len(valid) != len(ids): flash('Selected products must be active.', 'danger'); return redirect(url_for('admin_growth_tools'))
        bundle_value = sum((Decimal(str(p.price)) for p in valid), Decimal('0.00'))
        if price >= bundle_value:
            flash(f'Combo price must be below the regular bundle total of ₹{bundle_value:.2f}.', 'danger'); return redirect(url_for('admin_growth_tools'))
        import json
        db.session.add(ComboOffer(title=request.form['title'].strip()[:140], description=request.form.get('description','')[:500],
                                  product_ids=json.dumps([p.id for p in valid]), combo_price=price, active=True))
        db.session.commit(); flash(f'Combo offer created. Regular bundle total ₹{bundle_value:.2f}; combo price ₹{price:.2f}.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/growth-tools/combo/<int:combo_id>/toggle')
    @admin_required
    def admin_combo_toggle(combo_id):
        combo = ComboOffer.query.get_or_404(combo_id)
        combo.active = not combo.active
        db.session.commit()
        flash(f'Combo offer {combo.title} is now {"active" if combo.active else "inactive"}.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/growth-tools/flash-sale')
    @admin_required
    def admin_flash_sale_add():
        product = Product.query.get_or_404(request.form.get('product_id', type=int))
        price = money(request.form.get('sale_price'))
        try:
            start = datetime.fromisoformat(request.form.get('starts_at',''))
            end = datetime.fromisoformat(request.form.get('ends_at',''))
        except ValueError:
            flash('Enter valid sale start/end date and time.', 'danger'); return redirect(url_for('admin_growth_tools'))
        if price <= 0 or price >= product.price or end <= start:
            flash('Sale price must be below current price and end after start.', 'danger'); return redirect(url_for('admin_growth_tools'))
        db.session.add(FlashSale(product_id=product.id, sale_price=price, starts_at=start, ends_at=end))
        db.session.commit(); flash('Flash sale scheduled. Regular catalogue price is unchanged.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/growth-tools/flash-sale/<int:sale_id>/toggle')
    @admin_required
    def admin_flash_sale_toggle(sale_id):
        sale = FlashSale.query.get_or_404(sale_id)
        sale.active = not sale.active
        db.session.commit()
        flash(f'Flash sale for {sale.product.name} is now {"active" if sale.active else "inactive"}.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/memberships/<int:membership_id>/cancel')
    @admin_required
    def admin_membership_cancel(membership_id):
        membership = VipMembership.query.get_or_404(membership_id)
        membership.active = False
        db.session.commit()
        flash(f'{membership.tier} membership cancelled for {membership.user.name}.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.post('/admin/growth-tools/membership')
    @admin_required
    def admin_membership_grant():
        customer = User.query.filter_by(id=request.form.get('user_id', type=int), role='customer').first_or_404()
        tier = request.form.get('tier','Silver'); days = request.form.get('days',type=int) or 30
        if tier not in ['Silver','Gold','Platinum'] or not 1 <= days <= 365:
            flash('Choose a valid tier and 1–365 days.', 'danger'); return redirect(url_for('admin_growth_tools'))
        VipMembership.query.filter_by(user_id=customer.id, active=True).update({'active':False})
        db.session.add(VipMembership(user_id=customer.id,tier=tier,starts_at=datetime.utcnow(),ends_at=datetime.utcnow()+timedelta(days=days)))
        db.session.commit(); flash(f'{tier} membership activated for {customer.name}.', 'success')
        return redirect(url_for('admin_growth_tools'))

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

    @app.post('/admin/gift-cards/create')
    @admin_required
    def admin_gift_card_create():
        amount = money(request.form.get('amount'))
        if amount < Decimal('10.00') or amount > Decimal('10000.00'):
            flash('Gift card amount must be between ₹10 and ₹10,000.', 'warning')
            return redirect(url_for('admin_growth_tools'))
        code = 'TB-' + secrets.token_hex(5).upper()
        db.session.add(GiftCard(code=code, amount=amount, created_by=current_user.id))
        db.session.commit()
        flash(f'Gift card created: {code} · ₹{amount:.2f}. Share this code with the recipient.', 'success')
        return redirect(url_for('admin_growth_tools'))

    @app.route('/admin/orders', methods=['GET', 'POST'])
    @admin_required
    def admin_orders():
        if request.method == 'POST':
            order = Order.query.get_or_404(request.form.get('order_id', type=int))
            previous_status = order.status
            previous_partner_id = order.delivery_partner_id
            status = request.form.get('status', 'Pending')
            allowed = ['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Picked Up','Out for Delivery','Delivered','Cancelled']
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
            else:
                order.delivery_partner_id = None
            if status == 'Delivered' and order.delivery_partner_id:
                existing = DeliveryEarning.query.filter_by(order_id=order.id).first()
                if not existing:
                    db.session.add(DeliveryEarning(partner_id=order.delivery_partner_id, order_id=order.id, amount=Decimal('25.00')))
            # Give customers 1% wallet cashback once an order is first marked delivered.
            if status == 'Delivered' and previous_status != 'Delivered':
                cashback_reference = f'ORDER-CASHBACK-{order.id}'
                cashback_exists = WalletTransaction.query.filter_by(
                    user_id=order.user_id, kind='Credit', reference=cashback_reference
                ).first()
                if not cashback_exists:
                    cashback = (Decimal(str(order.subtotal or 0)) * Decimal('0.01')).quantize(Decimal('0.01'))
                    if cashback > 0:
                        db.session.add(WalletTransaction(
                            user_id=order.user_id, amount=cashback, kind='Credit',
                            reference=cashback_reference,
                            description=f'1% cashback for delivered order {order.order_number}'
                        ))
                        create_customer_notification(
                            order, 'Wallet cashback added',
                            f'₹{cashback:.2f} cashback has been added to your Tharai Basket wallet.'
                        )
            if status != previous_status:
                status_messages = {
                    'Confirmed': ('Order confirmed', 'Your order has been confirmed by the Tharai Basket team.'),
                    'Purchase Required': ('Purchase in progress', 'Our team is arranging items for your order.'),
                    'Purchased': ('Items purchased', 'Your order items have been purchased and are being prepared.'),
                    'Packing': ('Packing started', 'Your order is now being packed.'),
                    'Ready for Pickup': ('Ready for pickup', 'Your order is ready for the delivery partner to collect.'),
                    'Picked Up': ('Order picked up', 'The delivery partner has collected your order.'),
                    'Out for Delivery': ('Out for delivery', 'Your order is on the way!'),
                    'Delivered': ('Order delivered', 'Your Tharai Basket order has been delivered. Thank you!'),
                    'Cancelled': ('Order cancelled', 'Your order has been cancelled by the store team.'),
                }
                notice = status_messages.get(status)
                if notice:
                    create_customer_notification(order, notice[0], notice[1])
            if order.delivery_partner_id and order.delivery_partner_id != previous_partner_id:
                create_customer_notification(order, 'Delivery partner assigned', 'A delivery partner has been assigned to your order.')
            award_referral_points(order)
            if status == 'Delivered' and previous_status != 'Delivered':
                award_order_loyalty_points(order)
            db.session.commit()
            flash('Order updated.', 'success')
            return redirect(url_for('admin_orders'))
        orders = Order.query.order_by(Order.created_at.desc()).all()
        partners = User.query.filter_by(role='delivery', active=True).all()
        return render_template('admin/orders.html', orders=orders, partners=partners,
                               statuses=['Pending','Confirmed','Purchase Required','Purchased','Packing','Ready for Pickup','Picked Up','Out for Delivery','Delivered','Cancelled'])

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

    @app.post('/admin/delivery-partners/<int:partner_id>/toggle')
    @admin_required
    def admin_delivery_partner_toggle(partner_id):
        partner = User.query.filter_by(id=partner_id, role='delivery').first_or_404()
        partner.active = not bool(partner.active)
        db.session.commit()
        flash(f'{partner.name} account is now {"active" if partner.active else "disabled"}.', 'success')
        return redirect(url_for('admin_delivery_partners'))

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

    @app.get('/delivery/route')
    @delivery_required
    def delivery_route():
        active = Order.query.filter_by(delivery_partner_id=current_user.id).filter(
            Order.status.in_(['Ready for Pickup','Picked Up','Out for Delivery'])).order_by(Order.created_at.asc()).all()
        addresses = [o.address_text.replace('\n', ', ') for o in active if o.address_text]
        route_url = None
        if addresses:
            from urllib.parse import quote
            destination = quote(addresses[-1], safe='')
            origin = quote(addresses[0], safe='')
            waypoints = quote('|'.join(addresses[1:-1]), safe='')
            route_url = f'https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&travelmode=driving' + (f'&waypoints={waypoints}' if waypoints else '')
        return render_template('delivery/route.html', orders=active, route_url=route_url)

    @app.get('/delivery')
    @delivery_required
    def delivery_dashboard():
        all_orders = Order.query.filter_by(delivery_partner_id=current_user.id).order_by(Order.created_at.desc()).all()
        active_orders = [o for o in all_orders if o.status in ['Ready for Pickup', 'Picked Up', 'Out for Delivery']]
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
        next_status = {
            'Ready for Pickup': 'Picked Up',
            'Picked Up': 'Out for Delivery',
            'Out for Delivery': 'Delivered',
        }.get(order.status)
        if not next_status or status != next_status:
            flash('This order cannot move to that status. Follow the delivery steps in order.', 'warning')
            return redirect(url_for('delivery_dashboard'))
        order.status = status
        rider_status_messages = {
            'Picked Up': ('Order picked up', 'The delivery partner has collected your order.'),
            'Out for Delivery': ('Out for delivery', 'Your order is on the way!'),
            'Delivered': ('Order delivered', 'Your Tharai Basket order has been delivered. Thank you!'),
        }
        notice = rider_status_messages.get(status)
        if notice:
            create_customer_notification(order, notice[0], notice[1])
        if status == 'Delivered' and not DeliveryEarning.query.filter_by(order_id=order.id).first():
            db.session.add(DeliveryEarning(partner_id=current_user.id, order_id=order.id, amount=Decimal('25.00')))
        award_referral_points(order)
        if status == 'Delivered':
            award_order_loyalty_points(order)
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

        # Idempotently prepare an editable starter grocery catalogue.
        # Starter items are hidden and have placeholder prices/zero stock until the admin reviews them.
        starter_catalog = {
            'Rice & Grains': [
                ('Ponni Boiled Rice', '1 kg'), ('Idli Rice', '1 kg'), ('Raw Rice', '1 kg'),
                ('Basmati Rice', '1 kg'), ('Wheat Flour / Atta', '1 kg'), ('Maida', '1 kg'),
                ('Rava / Sooji', '500 g'), ('Rice Flour', '500 g'), ('Wheat Rava', '500 g'),
                ('Poha / Aval', '500 g'), ('Vermicelli / Semiya', '200 g')
            ],
            'Dals & Pulses': [
                ('Toor Dal', '1 kg'), ('Urad Dal', '500 g'), ('Moong Dal', '500 g'),
                ('Chana Dal', '500 g'), ('Masoor Dal', '500 g'), ('Whole Green Gram', '500 g'),
                ('White Chickpeas', '500 g'), ('Black Chickpeas', '500 g'), ('Rajma', '500 g')
            ],
            'Cooking Oils & Ghee': [
                ('Sunflower Oil', '1 L'), ('Groundnut Oil', '1 L'), ('Gingelly Oil', '500 ml'),
                ('Coconut Oil', '500 ml'), ('Rice Bran Oil', '1 L'), ('Refined Palm Oil', '1 L'),
                ('Ghee', '200 ml')
            ],
            'Spices & Masalas': [
                ('Chilli Powder', '100 g'), ('Turmeric Powder', '100 g'), ('Coriander Powder', '100 g'),
                ('Cumin Seeds', '100 g'), ('Mustard Seeds', '100 g'), ('Black Pepper', '50 g'),
                ('Fenugreek Seeds', '100 g'), ('Fennel Seeds', '100 g'), ('Jeera Powder', '100 g'),
                ('Sambar Powder', '100 g'), ('Rasam Powder', '100 g'), ('Garam Masala', '100 g'),
                ('Biryani Masala', '100 g'), ('Chicken Masala', '100 g'), ('Asafoetida / Hing', '50 g')
            ],
            'Salt, Sugar & Sweeteners': [
                ('White Sugar', '1 kg'), ('Jaggery', '500 g'), ('Iodised Salt', '1 kg'),
                ('Rock Salt', '500 g'), ('Crystal Salt', '1 kg')
            ],
            'Dairy & Bakery': [
                ('Milk', '500 ml'), ('Curd', '500 g'), ('Butter', '100 g'),
                ('Paneer', '200 g'), ('Cheese Slices', '200 g'), ('Bread', '1 pack'),
                ('Rusk', '200 g')
            ],
            'Beverages': [
                ('Tea Powder', '250 g'), ('Coffee Powder', '100 g'), ('Instant Coffee', '50 g'),
                ('Health Drink', '500 g'), ('Drinking Water', '1 L'), ('Fruit Drink', '1 L')
            ],
            'Snacks & Packaged Foods': [
                ('Mixture', '200 g'), ('Potato Chips', '1 pack'), ('Biscuits', '1 pack'),
                ('Cookies', '1 pack'), ('Papad', '200 g'), ('Appalam', '100 g'),
                ('Peanuts', '250 g'), ('Roasted Gram', '250 g'), ('Noodles', '1 pack'),
                ('Pasta', '500 g'), ('Tomato Ketchup', '500 g'), ('Pickle', '300 g')
            ],
            'Personal Care': [
                ('Bath Soap', '1 bar'), ('Shampoo', '180 ml'), ('Toothpaste', '150 g'),
                ('Toothbrush', '1 unit'), ('Hair Oil', '100 ml'), ('Face Wash', '100 ml'),
                ('Sanitary Napkins', '1 pack'), ('Hand Wash', '1 refill pack')
            ],
            'Home Cleaning': [
                ('Washing Powder', '1 kg'), ('Dishwash Bar', '1 bar'), ('Dishwash Liquid', '500 ml'),
                ('Floor Cleaner', '1 L'), ('Toilet Cleaner', '500 ml'), ('Phenyl', '1 L'),
                ('Scrub Pad', '1 unit'), ('Garbage Bags', '1 pack')
            ],
            'Household & Pooja Essentials': [
                ('Matchbox', '1 pack'), ('Candles', '1 pack'), ('Agarbathi', '1 pack'),
                ('Camphor', '1 pack'), ('Cotton Wicks', '1 pack'), ('Aluminium Foil', '1 roll'),
                ('Tissue Paper', '1 pack')
            ],
            'Baby Care': [
                ('Baby Soap', '1 bar'), ('Baby Shampoo', '100 ml'), ('Baby Wipes', '1 pack'),
                ('Baby Diapers', '1 pack')
            ]
        }
        existing_categories = {c.name: c for c in Category.query.all()}
        for category_name in starter_catalog:
            if category_name not in existing_categories:
                slug = '-'.join(category_name.lower().split())
                category = Category(name=category_name, slug=slug, active=True)
                db.session.add(category)
                existing_categories[category_name] = category
        db.session.flush()

        existing_product_names = {p.name.strip().casefold() for p in Product.query.all()}
        added_grocery_count = 0
        for category_name, items in starter_catalog.items():
            category = existing_categories[category_name]
            for product_name, unit in items:
                if product_name.casefold() in existing_product_names:
                    continue
                db.session.add(Product(
                    name=product_name,
                    brand='',
                    description='Starter catalogue item — admin to confirm brand, price and stock.',
                    unit=unit,
                    category=category,
                    mrp=Decimal('1.00'),
                    price=Decimal('1.00'),
                    stock=0,
                    active=False,
                    featured=False
                ))
                existing_product_names.add(product_name.casefold())
                added_grocery_count += 1
        if added_grocery_count:
            db.session.commit()
            app.logger.info("Added %s hidden starter grocery products for admin review.", added_grocery_count)

        # Ensure the launch offer exists without changing existing customer/order data.
        default_coupon = Coupon.query.filter_by(code='THARAI50').first()
        if not default_coupon:
            db.session.add(Coupon(code='THARAI50', discount_type='fixed', value=Decimal('50.00'),
                                  min_order=Decimal('500.00'), active=True))
            db.session.commit()

    return app

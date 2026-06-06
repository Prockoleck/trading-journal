import os
import imaplib
import email
import re
import html
from datetime import datetime, date
from email.header import decode_header
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

app = Flask(__name__)
app.secret_key = os.urandom(32)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///journal.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_mgr = LoginManager(app)
login_mgr.login_view = 'login'


email_config = {
    'imap_server': '',
    'email_addr': '',
    'email_pass': '',
    'last_checked': None,
}


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)
    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    volume = db.Column(db.Float, nullable=False)
    open_price = db.Column(db.Float, nullable=False)
    close_price = db.Column(db.Float, nullable=True)
    stop_loss = db.Column(db.Float, nullable=True)
    take_profit = db.Column(db.Float, nullable=True)
    profit = db.Column(db.Float, nullable=True)
    open_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    close_time = db.Column(db.DateTime, nullable=True)
    commission = db.Column(db.Float, default=0.0)
    swap = db.Column(db.Float, default=0.0)
    strategy = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    tags = db.Column(db.String(200), nullable=True)
    mt5_ticket = db.Column(db.String(50), unique=True, nullable=True)

    def duration(self):
        if not self.close_time:
            return None
        delta = self.close_time - self.open_time
        return delta

    def roi(self):
        if not self.profit or not self.volume or not self.open_price:
            return 0.0
        notional = self.volume * self.open_price * 100000
        if notional == 0:
            return 0.0
        return round((self.profit / notional) * 100, 4)

    def to_dict(self):
        return {
            'id': self.id,
            'symbol': self.symbol,
            'direction': self.direction,
            'volume': self.volume,
            'open_price': self.open_price,
            'close_price': self.close_price,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'profit': self.profit,
            'open_time': self.open_time.isoformat() if self.open_time else None,
            'close_time': self.close_time.isoformat() if self.close_time else None,
            'commission': self.commission,
            'swap': self.swap,
            'strategy': self.strategy,
            'notes': self.notes,
            'tags': self.tags,
            'mt5_ticket': self.mt5_ticket,
            'duration': str(self.duration()) if self.duration() else None,
            'roi': self.roi(),
        }


@login_mgr.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


@app.before_request
def create_tables():
    db.create_all()
    if not User.query.first():
        admin = User(username='admin')
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    total_trades = Trade.query.count()
    winning = Trade.query.filter(Trade.profit.isnot(None), Trade.profit > 0).count()
    losing = Trade.query.filter(Trade.profit.isnot(None), Trade.profit < 0).count()
    total_profit = db.session.query(func.sum(Trade.profit)).scalar() or 0.0
    win_rate = round((winning / total_trades * 100), 1) if total_trades > 0 else 0

    best_trade = Trade.query.filter(Trade.profit.isnot(None)).order_by(Trade.profit.desc()).first()
    worst_trade = Trade.query.filter(Trade.profit.isnot(None)).order_by(Trade.profit.asc()).first()

    recent = Trade.query.order_by(Trade.open_time.desc()).limit(10).all()

    top_pairs = (
        db.session.query(Trade.symbol, func.count(Trade.id).label('cnt'))
        .group_by(Trade.symbol)
        .order_by(func.count(Trade.id).desc())
        .limit(5)
        .all()
    )

    monthly = (
        db.session.query(
            func.strftime('%Y-%m', Trade.close_time).label('month'),
            func.sum(Trade.profit).label('pnl'),
            func.count(Trade.id).label('count'),
        )
        .filter(Trade.close_time.isnot(None))
        .group_by('month')
        .order_by('month')
        .all()
    )

    return render_template(
        'dashboard.html',
        total_trades=total_trades,
        winning=winning,
        losing=losing,
        total_profit=round(total_profit, 2),
        win_rate=win_rate,
        best_trade=best_trade,
        worst_trade=worst_trade,
        recent=recent,
        top_pairs=top_pairs,
        monthly=monthly,
    )


@app.route('/trades')
@login_required
def trade_list():
    page = request.args.get('page', 1, type=int)
    symbol_filter = request.args.get('symbol', '')
    direction_filter = request.args.get('direction', '')
    sort = request.args.get('sort', '-open_time')

    q = Trade.query
    if symbol_filter:
        q = q.filter(Trade.symbol.ilike(f'%{symbol_filter}%'))
    if direction_filter:
        q = q.filter(Trade.direction == direction_filter)

    if sort.startswith('-'):
        col = getattr(Trade, sort[1:], Trade.open_time)
        q = q.order_by(col.desc())
    else:
        col = getattr(Trade, sort, Trade.open_time)
        q = q.order_by(col.asc())

    trades = q.paginate(page=page, per_page=25, error_out=False)
    symbols = [r[0] for r in db.session.query(Trade.symbol).distinct().all()]
    return render_template('trades.html', trades=trades, symbols=symbols,
                           symbol_filter=symbol_filter, direction_filter=direction_filter, sort=sort)


@app.route('/trades/add', methods=['GET', 'POST'])
@login_required
def add_trade():
    if request.method == 'POST':
        try:
            open_time = datetime.strptime(request.form['open_time'], '%Y-%m-%dT%H:%M') if request.form.get('open_time') else datetime.utcnow()
            close_time = datetime.strptime(request.form['close_time'], '%Y-%m-%dT%H:%M') if request.form.get('close_time') else None
        except ValueError:
            open_time = datetime.utcnow()
            close_time = None

        trade = Trade(
            symbol=request.form['symbol'].upper(),
            direction=request.form['direction'],
            volume=float(request.form['volume']),
            open_price=float(request.form['open_price']),
            close_price=float(request.form['close_price']) if request.form.get('close_price') else None,
            stop_loss=float(request.form['stop_loss']) if request.form.get('stop_loss') else None,
            take_profit=float(request.form['take_profit']) if request.form.get('take_profit') else None,
            profit=float(request.form['profit']) if request.form.get('profit') else None,
            open_time=open_time,
            close_time=close_time,
            commission=float(request.form.get('commission', 0)),
            swap=float(request.form.get('swap', 0)),
            strategy=request.form.get('strategy'),
            notes=request.form.get('notes'),
            tags=request.form.get('tags'),
        )
        if trade.profit is None and trade.close_price and trade.open_price:
            diff = trade.close_price - trade.open_price
            if trade.direction == 'sell':
                diff = -diff
            trade.profit = round(diff * trade.volume * 100000, 2)

        db.session.add(trade)
        db.session.commit()
        flash('Trade added')
        return redirect(url_for('trade_list'))
    return render_template('add_trade.html', trade=None)


@app.route('/trades/<int:tid>/edit', methods=['GET', 'POST'])
@login_required
def edit_trade(tid):
    trade = db.session.get(Trade, tid)
    if not trade:
        flash('Trade not found')
        return redirect(url_for('trade_list'))
    if request.method == 'POST':
        trade.symbol = request.form['symbol'].upper()
        trade.direction = request.form['direction']
        trade.volume = float(request.form['volume'])
        trade.open_price = float(request.form['open_price'])
        trade.close_price = float(request.form['close_price']) if request.form.get('close_price') else None
        trade.stop_loss = float(request.form['stop_loss']) if request.form.get('stop_loss') else None
        trade.take_profit = float(request.form['take_profit']) if request.form.get('take_profit') else None
        trade.profit = float(request.form['profit']) if request.form.get('profit') else None
        try:
            trade.open_time = datetime.strptime(request.form['open_time'], '%Y-%m-%dT%H:%M') if request.form.get('open_time') else trade.open_time
            trade.close_time = datetime.strptime(request.form['close_time'], '%Y-%m-%dT%H:%M') if request.form.get('close_time') else None
        except ValueError:
            pass
        trade.commission = float(request.form.get('commission', 0))
        trade.swap = float(request.form.get('swap', 0))
        trade.strategy = request.form.get('strategy')
        trade.notes = request.form.get('notes')
        trade.tags = request.form.get('tags')
        db.session.commit()
        flash('Trade updated')
        return redirect(url_for('trade_list'))
    return render_template('add_trade.html', trade=trade)


@app.route('/trades/<int:tid>/delete', methods=['POST'])
@login_required
def delete_trade(tid):
    trade = db.session.get(Trade, tid)
    if trade:
        db.session.delete(trade)
        db.session.commit()
    return redirect(url_for('trade_list'))


@app.route('/trades/import-csv', methods=['POST'])
@login_required
def import_csv():
    file = request.files.get('file')
    if not file:
        flash('No file uploaded')
        return redirect(url_for('trade_list'))
    import csv
    import io
    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    count = 0
    for row in reader:
        try:
            profit = float(row.get('Profit', 0)) if row.get('Profit') else None
            trade = Trade(
                symbol=row.get('Symbol', '').upper(),
                direction=row.get('Direction', 'buy').lower(),
                volume=float(row.get('Volume', 0.01)),
                open_price=float(row.get('Open Price', 0)),
                close_price=float(row.get('Close Price', 0)) if row.get('Close Price') else None,
                profit=profit,
                open_time=datetime.strptime(row['Open Time'], '%Y-%m-%d %H:%M:%S') if row.get('Open Time') else datetime.utcnow(),
                close_time=datetime.strptime(row['Close Time'], '%Y-%m-%d %H:%M:%S') if row.get('Close Time') else None,
                commission=float(row.get('Commission', 0)),
                swap=float(row.get('Swap', 0)),
                strategy=row.get('Strategy'),
                tags=row.get('Tags'),
                mt5_ticket=row.get('Ticket'),
            )
            db.session.add(trade)
            count += 1
        except (ValueError, KeyError) as e:
            flash(f'Skipped row: {e}')
    db.session.commit()
    flash(f'Imported {count} trades')
    return redirect(url_for('trade_list'))


def parse_mt5_email(email_body):
    trade = {}
    lines = email_body.split('\n')
    for line in lines:
        line = line.strip()
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip().lower()
        val = val.strip()

        if 'ticket' in key:
            trade['ticket'] = val.split('#')[-1].strip()
        elif 'symbol' in key:
            trade['symbol'] = val.split()[-1].upper()
        elif 'position' in key and ('buy' in val.lower() or 'sell' in val.lower()):
            trade['direction'] = 'buy' if 'buy' in val.lower() else 'sell'
            m = re.search(r'([\d.]+)\s*lot', val.lower())
            if m:
                trade['volume'] = float(m.group(1))
        elif 'volume' in key or 'lot' in key:
            m = re.search(r'[\d.]+', val)
            if m:
                trade['volume'] = float(m.group())
        elif 'open price' in key or 'price' in key:
            m = re.search(r'[\d.]+', val)
            if m:
                trade['open_price'] = float(m.group())
        elif 'close price' in key:
            m = re.search(r'[\d.]+', val)
            if m:
                trade['close_price'] = float(m.group())
        elif 'stop loss' in key or 'sl' in key:
            m = re.search(r'[\d.]+', val)
            if m:
                trade['stop_loss'] = float(m.group())
        elif 'take profit' in key or 'tp' in key:
            m = re.search(r'[\d.]+', val)
            if m:
                trade['take_profit'] = float(m.group())
        elif 'profit' in key:
            m = re.search(r'-?[\d.]+', val.replace(',', ''))
            if m:
                trade['profit'] = float(m.group())
        elif 'open time' in key or 'time' in key:
            try:
                trade['open_time'] = datetime.strptime(val, '%Y.%m.%d %H:%M:%S')
            except ValueError:
                pass
        elif 'close time' in key:
            try:
                trade['close_time'] = datetime.strptime(val, '%Y.%m.%d %H:%M:%S')
            except ValueError:
                pass
        elif 'commission' in key:
            m = re.search(r'-?[\d.]+', val.replace(',', ''))
            if m:
                trade['commission'] = abs(float(m.group()))
        elif 'swap' in key:
            m = re.search(r'-?[\d.]+', val.replace(',', ''))
            if m:
                trade['swap'] = float(m.group())
        elif 'comment' in key:
            trade['strategy'] = val

    return trade


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    global email_config
    if request.method == 'POST':
        email_config['imap_server'] = request.form.get('imap_server', '')
        email_config['email_addr'] = request.form.get('email_addr', '')
        email_config['email_pass'] = request.form.get('email_pass', '')
        flash('Settings saved')
        return redirect(url_for('settings'))
    return render_template('settings.html', config=email_config)


@app.route('/import-email')
@login_required
def import_email():
    global email_config
    if not email_config.get('email_addr') or not email_config.get('email_pass'):
        flash('Configure email settings first')
        return redirect(url_for('settings'))

    try:
        mail = imaplib.IMAP4_SSL(email_config['imap_server'])
        mail.login(email_config['email_addr'], email_config['email_pass'])
        mail.select('INBOX')

        search_criteria = '(FROM "metaquotes" OR FROM "mt5" OR SUBJECT "Trade" OR SUBJECT "Order" OR SUBJECT "Deal")'
        status, ids = mail.search(None, search_criteria)
        if status != 'OK' or not ids[0]:
            flash('No matching emails found')
            mail.logout()
            return redirect(url_for('trade_list'))

        id_list = ids[0].split()
        count = 0
        for mid in id_list[-50:]:
            status, data = mail.fetch(mid, '(RFC822)')
            if status != 'OK':
                continue
            raw = email.message_from_bytes(data[0][1])
            if raw.is_multipart():
                body = ''
                for part in raw.walk():
                    if part.get_content_type() == 'text/plain':
                        try:
                            body += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        except Exception:
                            pass
            else:
                try:
                    body = raw.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception:
                    body = ''

            parsed = parse_mt5_email(body)
            if not parsed.get('symbol') or not parsed.get('volume'):
                continue
            existing = Trade.query.filter_by(mt5_ticket=parsed.get('ticket')).first()
            if existing:
                continue
            trade = Trade(
                symbol=parsed.get('symbol', 'UNKNOWN'),
                direction=parsed.get('direction', 'buy'),
                volume=parsed.get('volume', 0.01),
                open_price=parsed.get('open_price', 0),
                close_price=parsed.get('close_price'),
                profit=parsed.get('profit'),
                open_time=parsed.get('open_time', datetime.utcnow()),
                close_time=parsed.get('close_time'),
                stop_loss=parsed.get('stop_loss'),
                take_profit=parsed.get('take_profit'),
                commission=parsed.get('commission', 0),
                swap=parsed.get('swap', 0),
                strategy=parsed.get('strategy'),
                mt5_ticket=parsed.get('ticket'),
            )
            db.session.add(trade)
            count += 1

        db.session.commit()
        mail.logout()
        flash(f'Imported {count} trades from email')
    except Exception as e:
        flash(f'Email import error: {e}')
    return redirect(url_for('trade_list'))


@app.route('/api/trades')
@login_required
def api_trades():
    trades = Trade.query.order_by(Trade.open_time.desc()).all()
    return jsonify([t.to_dict() for t in trades])


@app.route('/api/stats')
@login_required
def api_stats():
    total = Trade.query.count()
    winning = Trade.query.filter(Trade.profit.isnot(None), Trade.profit > 0).count()
    losing = Trade.query.filter(Trade.profit.isnot(None), Trade.profit < 0).count()
    total_profit = db.session.query(func.sum(Trade.profit)).scalar() or 0.0
    return jsonify({
        'total_trades': total,
        'winning': winning,
        'losing': losing,
        'total_profit': round(total_profit, 2),
        'win_rate': round((winning / total * 100), 1) if total > 0 else 0,
    })


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.first():
            u = User(username='admin')
            u.set_password('admin')
            db.session.add(u)
            db.session.commit()
    app.run(host='0.0.0.0', port=5000, debug=True)

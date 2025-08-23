from flask import Flask, render_template, request, redirect, url_for, flash, get_flashed_messages, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os
import cloudinary
import cloudinary.uploader
import cloudinary.api
from functools import wraps
import time
from dotenv import load_dotenv

load_dotenv()

from extensions import db
from models import FamilyMember, Comment, MemorableMoment, User

app = Flask(__name__)
app.secret_key = os.getenv('OGBONNA_SECRET_KEY')

# Session timeout configuration (30 minutes)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Configure the app
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"  # This is correct for Flask-Login

bcrypt = Bcrypt(app)

# Database configuration with connection pooling
database_url = os.getenv('SUPABASE_DATABASE_URL', 'sqlite:///family.db')
if database_url.startswith('postgres'):
    database_url = database_url.replace('postgres://', 'postgresql://')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 1800,  # 30 minutes
        'pool_size': 3,
        'max_overflow': 5,
        'pool_timeout': 20,
        'connect_args': {
            'connect_timeout': 10,
            'application_name': 'ogbonna_family_app',
            'options': '-c statement_timeout=30000'
        }
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 1800,
    }

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAIL_SERVER'] = 'smtp.sendgrid.net'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'apikey'  # This is literally the string 'apikey'
app.config['MAIL_PASSWORD'] = os.environ.get('SENDGRID_API_KEY')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER')

# Cloudinary Configuration
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# ✅ Ensure upload folder exists (even in production)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize database
db.init_app(app)
mail = Mail(app)

# Database connection cleanup function
def cleanup_db_connection():
    """Clean up database connections to prevent connection leaks"""
    try:
        db.session.remove()
        db.engine.dispose()
    except Exception as e:
        print(f"Error cleaning up database connection: {e}")

def log_connection_pool_status():
    """Log the current status of the database connection pool"""
    try:
        engine = db.engine
        pool = engine.pool
        print(f"Connection pool status - Size: {pool.size()}, Checked out: {pool.checkedout()}, Overflow: {pool.overflow()}")
    except Exception as e:
        print(f"Error logging connection pool status: {e}")

def retry_db_operation(operation, max_retries=3, delay=1):
    """Retry a database operation with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return operation()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            print(f"Database operation failed (attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(delay * (2 ** attempt))  # Exponential backoff
            cleanup_db_connection()

# Ensure tables are created (production-safe) - This is now handled by migrations
# with app.app_context():
#     db.create_all()

# Allowed file extensions
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def upload_to_cloudinary(file, folder='ogbonna_family'):
    """Upload a file to Cloudinary"""
    try:
        if file and allowed_file(file.filename):
            # Upload to Cloudinary
            result = cloudinary.uploader.upload(
                file,
                folder=folder,
                resource_type="auto"
            )
            return result['public_id']  # Return the public_id for storage
    except Exception as e:
        print(f"Error uploading to Cloudinary: {e}")
        return None
    return None

def get_cloudinary_url(public_id, transformation=None):
    """Get the URL for a file in Cloudinary"""
    if public_id:
        try:
            if transformation:
                return cloudinary.CloudinaryImage(public_id).build_url(transformation=transformation)
            else:
                return cloudinary.CloudinaryImage(public_id).build_url()
        except Exception as e:
            print(f"Error getting Cloudinary URL: {e}")
            return None
    return None

def delete_from_cloudinary(public_id):
    """Delete a file from Cloudinary"""
    if public_id:
        try:
            result = cloudinary.uploader.destroy(public_id)
            return result.get('result') == 'ok'
        except Exception as e:
            print(f"Error deleting from Cloudinary: {e}")
            return False
    return False

# Make get_cloudinary_url available in templates
@app.context_processor
def utility_processor():
    return dict(get_cloudinary_url=get_cloudinary_url)

def can_edit_member(member):
    """Check if current user can edit this family member"""
    if not current_user.is_authenticated:
        return False
    return member.created_by == current_user.id

def can_delete_member(member):
    """Check if current user can delete this family member"""
    if not current_user.is_authenticated:
        return False
    return member.created_by == current_user.id

def require_member_ownership(f):
    """Decorator to require ownership of a family member"""
    @wraps(f)
    def decorated_function(member_id, *args, **kwargs):
        member = FamilyMember.query.get_or_404(member_id)
        if not can_edit_member(member):
            flash('You do not have permission to perform this action.', category='error')
            return redirect(url_for('index'))
        return f(member_id, *args, **kwargs)
    return decorated_function

@app.route('/init-db')
def init_db():
    with app.app_context():
        db.create_all()
    return "Database tables created successfully!"

@app.route('/health')
def health_check():
    """Health check endpoint to monitor database connectivity"""
    try:
        # Test database connection
        db.session.execute('SELECT 1')
        db.session.commit()
        return {'status': 'healthy', 'database': 'connected'}, 200
    except Exception as e:
        cleanup_db_connection()
        return {'status': 'unhealthy', 'database': 'disconnected', 'error': str(e)}, 500

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

from flask_login import login_user, login_required, logout_user, current_user

def check_session_timeout():
    """Check if user session has expired due to inactivity"""
    if current_user.is_authenticated:
        # Get last activity time from session
        last_activity = session.get('last_activity')
        if last_activity:
            last_activity = datetime.fromisoformat(last_activity)
            # Check if more than 30 minutes have passed
            if datetime.utcnow() - last_activity > timedelta(minutes=30):
                logout_user()
                session.clear()
                flash('Your session has expired due to inactivity. Please log in again.', category='info')
                return True
        # Update last activity time
        session['last_activity'] = datetime.utcnow().isoformat()
    return False

@app.before_request
def before_request():
    """Run before each request to check session timeout"""
    check_session_timeout()
    
    # Log connection pool status every 100 requests (roughly)
    import random
    if random.randint(1, 100) == 1:
        log_connection_pool_status()

@app.teardown_appcontext
def teardown_db(exception):
    """Clean up database connections after each request"""
    cleanup_db_connection()
    
    # Force cleanup of any remaining connections
    try:
        db.session.close()
        db.engine.dispose()
    except Exception as e:
        print(f"Error in teardown: {e}")

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password1 = request.form['password1']
        password2 = request.form['password2']

        user = User.query.filter_by(username=username).first()
        email_user = User.query.filter_by(email=email).first()
        if user:
            flash('Username already exists.', category='error')
        elif email_user:
            flash('Email already registered.', category='error')
        elif len(username) < 2:
            flash('Username must not be left blank and must be greater than or equal to 2 characters.', category='error')
        elif password1 != password2:
            flash('Passwords do not match.', category='error')
        elif len(password1) < 7:
            flash('Password must be at least 7 characters.', category='error')
        else:
            new_user = User(username=username, email=email, password_hash=bcrypt.generate_password_hash(password1).decode('utf-8'))
            db.session.add(new_.user)
            db.session.commit()
            
            # Set session as permanent and initialize last activity
            session.permanent = True
            session['last_activity'] = datetime.utcnow().isoformat()
            
            login_user(new_user, remember=True)
            flash('Account created!', category='success')
            return redirect(url_for('login'))

    return render_template('register.html', user=current_user)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()

        if user and bcrypt.check_password_hash(user.password_hash, password):
            # Set session as permanent and initialize last activity
            session.permanent = True
            session['last_activity'] = datetime.utcnow().isoformat()
            
            flash(f'Logged in successfully! Welcome to the Ogbonna\'s Family Website, {user.username}', category='success')
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password.', category='error')

    return render_template('login.html', user=current_user)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('login'))


@app.route('/extend-session', methods=['POST'])
@login_required
def extend_session():
    """Extend user session by updating last activity time"""
    session['last_activity'] = datetime.utcnow().isoformat()
    return {'status': 'success'}, 200


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        
        if user:
            # Generate reset token and code
            reset_code = user.generate_reset_token()
            db.session.commit()
            
            # Send email with reset code
            try:
                msg = Message(
                    subject='Password Reset Code - Ogbonna Family Website',
                    recipients=[user.email],
                    body=f'''Hello {user.username},

You have requested to reset your password for the Ogbonna Family Website.

Your reset code is: {reset_code}

This code will expire in 15 minutes.

If you did not request this password reset, please ignore this email.

Best regards,
Ogbonna Family Website Team'''
                )
                mail.send(msg)
                flash('A reset code has been sent to your email address.', category='success')
            except Exception as e:
                print(f"Error sending email: {e}")
                flash('Error sending reset code. Please try again.', category='error')
        else:
            # Don't reveal if email exists or not for security
            flash('If an account with that email exists, a reset code has been sent.', category='info')
        
        return redirect(url_for('verify_reset_code'))
    
    return render_template('forgot_password.html')


@app.route('/verify-reset-code', methods=['GET', 'POST'])
def verify_reset_code():
    if request.method == 'POST':
        email = request.form['email']
        reset_code = request.form['reset_code']
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.is_reset_token_valid() and user.reset_code == reset_code:
            # Store email in session for password reset
            session['reset_email'] = email
            flash('Code verified successfully! Please enter your new password.', category='success')
            return redirect(url_for('reset_password'))
        else:
            flash('Invalid or expired reset code. Please try again.', category='error')
            return redirect(url_for('forgot_password'))
    
    return render_template('verify_reset_code.html')


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if 'reset_email' not in session:
        flash('Please request a password reset first.', category='error')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        email = session['reset_email']
        password1 = request.form['password1']
        password2 = request.form['password2']
        
        user = User.query.filter_by(email=email).first()
        
        if not user or not user.is_reset_token_valid():
            flash('Invalid or expired reset session. Please try again.', category='error')
            session.pop('reset_email', None)
            return redirect(url_for('forgot_password'))
        
        if password1 != password2:
            flash('Passwords do not match.', category='error')
        elif len(password1) < 7:
            flash('Password must be at least 7 characters.', category='error')
        else:
            # Update password and clear reset token
            user.password_hash = bcrypt.generate_password_hash(password1).decode('utf-8')
            user.clear_reset_token()
            db.session.commit()
            
            # Clear session
            session.pop('reset_email', None)
            
            flash('Password has been reset successfully! You can now log in with your new password.', category='success')
            return redirect(url_for('login'))
    
    return render_template('reset_password.html')


@app.route('/')
def index():
    relationship = request.args.get('relationship', '')
    if relationship:
        members = FamilyMember.query.filter(
            FamilyMember.relationship.ilike(f"%{relationship}%")
        ).all()
    else:
        members = FamilyMember.query.all()

    # Birthday reminder logic
    today = datetime.today()
    birthday_members = [member for member in members if member.dob.month == today.month and member.dob.day == today.day]
    birthday_notifications = [f"Today is {member.full_name}'s birthday! 🎉" for member in birthday_members]

    # Motivational quotes
    motivational_quotes = [
        "Family is not an important thing. It's everything.",
        "The love of a family is life's greatest blessing.",
        "Family: where life begins and love never ends.",
        "Rejoice with your family in the beautiful land of life.",
        "A happy family is but an earlier heaven.",
        "In time of test, family is best.",
        "The memories we make with our family is everything.", 
        "Family is your first team—always play for each other.", 
        "A strong family builds strong hearts.", 
        "Love begins at home—nurture it daily.", 
        "Together, we are unbreakable.", 
        "Home is where support never ends.", 
        "Family: your forever source of strength.", 
        "Grow together, rise together.", 
        "In unity, we find peace.", 
        "Family fuels your dreams with love.", 
        "With family by your side, anything is possible."
    ]

    show_notifications = birthday_notifications if birthday_notifications else motivational_quotes

    # Get memorable moments for slideshow
    memorable_moments = MemorableMoment.query.order_by(MemorableMoment.posted_at.desc()).all()

    # Email notification to all users if there is a birthday today
    if birthday_members:
        users = User.query.all()
        recipient_emails = [user.email for user in users if user.email]
        for member in birthday_members:
            msg = Message(
                subject=f"Birthday Reminder: {member.full_name}",
                recipients=recipient_emails,
                body=f"Today is {member.full_name}'s birthday! Wish them a happy birthday!"
            )
            mail.send(msg)

    return render_template('home.html', members=members, relationship=relationship, notifications=show_notifications, memorable_moments=memorable_moments)


# View a family member's profile
@app.route('/member/<int:member_id>', methods=['GET', 'POST'])
def member_profile(member_id):
    member = FamilyMember.query.get_or_404(member_id)
    if request.method == 'POST':
        name = request.form['name']
        content = request.form['content']
        if name and content:
            comment = Comment(member_id=member.id, name=name, content=content)
            db.session.add(comment)
            db.session.commit()
            flash('Comment added!', category='success')
        else:
            flash('Name and comment are required.', category='error')
        return redirect(url_for('member_profile', member_id=member.id))
    comments = Comment.query.filter_by(member_id=member.id).order_by(Comment.timestamp.desc()).all()
    return render_template('member_profile.html', member=member, comments=comments)


# Add a new family member
@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_member():
    if request.method == 'POST':
        full_name = request.form['full_name']
        dob = datetime.strptime(request.form['dob'], '%Y-%m-%d')
        dod = request.form['dod']
        is_alive = not bool(dod.strip())
        dod = datetime.strptime(dod, '%Y-%m-%d') if dod.strip() else None
        biography = request.form['biography']
        relationship = request.form['relationship']
        photo = request.files['photo']
        photo_filename = None

        parent_id = request.form.get('parent_id')
        parent_id = int(parent_id) if parent_id else None

        if photo and photo.filename != '':
            photo_filename = upload_to_cloudinary(photo)
            if not photo_filename:
                flash('Error saving photo. Please try again.', category='error')
                return redirect(url_for('add_member'))

        new_member = FamilyMember(
            full_name=full_name,
            dob=dob,
            dod=dod,
            is_alive=is_alive,
            biography=biography,
            relationship=relationship,
            photo_url=photo_filename,
            parent_id=parent_id,
            created_by=current_user.id  # Set the creator
        )

        db.session.add(new_member)
        db.session.commit()
        flash('Family member added successfully!', category='success')
        return redirect(url_for('index'))

    # Get all family members for parent selection
    family_members = FamilyMember.query.all()
    return render_template('add_member.html', family_members=family_members)


# Edit a family member
@app.route('/edit/<int:member_id>', methods=['GET', 'POST'])
@login_required
@require_member_ownership
def edit_member(member_id):
    member = FamilyMember.query.get_or_404(member_id)

    if request.method == 'POST':
        member.full_name = request.form['full_name']
        member.dob = datetime.strptime(request.form['dob'], '%Y-%m-%d')
        dod = request.form['dod']
        member.is_alive = not bool(dod.strip())
        member.dod = datetime.strptime(dod, '%Y-%m-%d') if dod.strip() else None
        member.biography = request.form['biography']
        member.relationship = request.form['relationship']

        parent_id = request.form.get('parent_id')
        parent_id = int(parent_id) if parent_id else None

        photo = request.files['photo']
        if photo and photo.filename != '':
            # Delete old photo if it exists
            if member.photo_url:
                delete_from_cloudinary(member.photo_url)
            
            photo_filename = upload_to_cloudinary(photo)
            if not photo_filename:
                flash('Error saving photo. Please try again.', category='error')
                return redirect(url_for('edit_member', member_id=member.id))
            
            member.photo_url = photo_filename

        db.session.commit()
        flash('Family member updated successfully!', category='success')
        return redirect(url_for('member_profile', member_id=member.id))

    # Get all family members for parent selection
    family_members = FamilyMember.query.all()
    return render_template('edit_member.html', family_members=family_members, member=member)


# Delete a family member
@app.route('/delete/<int:member_id>', methods=['POST'])
@login_required
@require_member_ownership
def delete_member(member_id):
    member = FamilyMember.query.get_or_404(member_id)

    # Delete photo file from cloudinary
    if member.photo_url:
        delete_from_cloudinary(member.photo_url)

    db.session.delete(member)
    db.session.commit()
    flash('Family member deleted successfully!', category='success')
    return redirect(url_for('index'))


# Generate family tree
@app.route('/family-tree')
def family_tree():
    members = FamilyMember.query.all()
    member_dict = {m.id: m for m in members}
    # Build a set of all member ids that are children
    child_ids = set(m.parent_id for m in members if m.parent_id)
    # Roots are members who are not anyone's child (orphaned or true roots)
    roots = [m for m in members if m.id not in child_ids or not m.parent_id]

    def build_tree(member):
        return {
            "name": member.full_name,
            "children": [build_tree(child) for child in member.children]
        }

    tree_data = [build_tree(root) for root in roots]
    return render_template('family_tree.html', tree_data=tree_data)


@app.route('/post-moment', methods=['GET', 'POST'])
@login_required
def post_moment():
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        image = request.files['image']
        
        if not title or not image:
            flash('Title and image are required.', category='error')
            return redirect(url_for('post_moment'))
        
        if image and image.filename != '':
            image_filename = upload_to_cloudinary(image, 'moments')
            if not image_filename:
                flash('Error saving image. Please try again.', category='error')
                return redirect(url_for('post_moment'))
            
            moment = MemorableMoment(
                title=title,
                description=description,
                image_url=image_filename,
                posted_by=current_user.id
            )
            db.session.add(moment)
            db.session.commit()
            flash('Memorable moment posted successfully!', category='success')
            return redirect(url_for('index'))
    
    return render_template('post_moment.html')

@app.route('/delete-moment/<int:moment_id>', methods=['POST'])
@login_required
def delete_moment(moment_id):
    moment = MemorableMoment.query.get_or_404(moment_id)
    
    # Only allow the user who posted it to delete it
    if moment.posted_by != current_user.id:
        flash('You can only delete your own posts.', category='error')
        return redirect(url_for('index'))
    
    # Delete image from local storage
    if moment.image_url:
        delete_from_cloudinary(moment.image_url)
    
    db.session.delete(moment)
    db.session.commit()
    flash('Memorable moment deleted successfully!', category='success')
    return redirect(url_for('index'))

# Run the app
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', debug=True, port=5000)

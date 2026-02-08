"""
Authentication and User Management Controller
"""

from py4web import action, request, abort, redirect, URL, response, session
from py4web.utils.form import Form, FormStyleBootstrap4
from py4web.utils.auth import Auth
from pydal.validators import *
from ..models import db
import json
import hashlib
import secrets
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Initialize auth
auth = Auth(session, db)

@action("auth/login")
@action.uses("auth/login.html", auth)
def login():
    """User login page"""
    
    if auth.current_user:
        redirect(URL('index'))
    
    form = Form([
        Field('username', 'string', length=255,
              requires=IS_NOT_EMPTY(),
              placeholder='Username'),
        Field('password', 'password',
              requires=IS_NOT_EMPTY(),
              placeholder='Password'),
        Field('remember_me', 'boolean',
              label='Remember me')
    ], formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        username = form.vars.username
        password = form.vars.password
        remember_me = form.vars.remember_me
        
        # Find user
        user = db(db.auth_user.username == username).select().first()
        
        if user and verify_password(password, user.password_hash):
            # Check if user is active
            if not user.is_active:
                form.errors['username'] = 'Account is disabled'
            else:
                # Log successful login
                db.auth_login_attempts.insert(
                    username=username,
                    ip_address=request.environ.get('REMOTE_ADDR', ''),
                    success=True,
                    attempted_on=datetime.utcnow()
                )
                
                # Update last login
                db(db.auth_user.id == user.id).update(
                    last_login=datetime.utcnow(),
                    login_count=(user.login_count or 0) + 1
                )
                
                # Set session
                auth.login_user(user)
                
                # Set remember me cookie if requested
                if remember_me:
                    response.set_cookie('remember_token', 
                                      generate_remember_token(user.id),
                                      max_age=30*24*3600)  # 30 days
                
                db.commit()
                
                # Log system event
                db.system_logs.insert(
                    level='INFO',
                    component='auth',
                    message=f'User {username} logged in successfully',
                    metadata=json.dumps({
                        'user_id': user.id,
                        'username': username,
                        'ip_address': request.environ.get('REMOTE_ADDR', '')
                    })
                )
                
                redirect(URL('index'))
        else:
            # Log failed login attempt
            db.auth_login_attempts.insert(
                username=username,
                ip_address=request.environ.get('REMOTE_ADDR', ''),
                success=False,
                attempted_on=datetime.utcnow()
            )
            db.commit()
            
            form.errors['password'] = 'Invalid username or password'
    
    return {'form': form}

@action("auth/logout")
@action.uses(auth)
def logout():
    """User logout"""
    
    if auth.current_user:
        # Log logout
        db.system_logs.insert(
            level='INFO',
            component='auth',
            message=f'User {auth.current_user.username} logged out',
            metadata=json.dumps({
                'user_id': auth.current_user.id,
                'username': auth.current_user.username
            })
        )
        db.commit()
    
    # Clear remember me cookie
    response.set_cookie('remember_token', '', max_age=0)
    
    # Logout user
    auth.logout()
    
    redirect(URL('auth/login'))

@action("auth/profile")
@action.uses("auth/profile.html", auth.user, db)
def profile():
    """User profile management"""
    
    user = auth.current_user
    
    # Profile update form
    form = Form([
        Field('first_name', 'string', length=255,
              default=user.first_name,
              placeholder='First Name'),
        Field('last_name', 'string', length=255,
              default=user.last_name,
              placeholder='Last Name'),
        Field('email', 'string', length=255,
              requires=IS_EMAIL(),
              default=user.email,
              placeholder='Email'),
        Field('timezone', 'string', length=100,
              default=user.timezone or 'UTC',
              requires=IS_IN_SET([
                  'UTC', 'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
                  'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Asia/Tokyo'
              ]))
    ], formstyle=FormStyleBootstrap4)
    
    # Password change form
    password_form = Form([
        Field('current_password', 'password',
              requires=IS_NOT_EMPTY(),
              placeholder='Current Password'),
        Field('new_password', 'password',
              requires=[IS_NOT_EMPTY(), IS_LENGTH(8, 255)],
              placeholder='New Password'),
        Field('confirm_password', 'password',
              requires=IS_NOT_EMPTY(),
              placeholder='Confirm New Password')
    ], formstyle=FormStyleBootstrap4, formname='password_form')
    
    if form.accepted:
        # Update profile
        db(db.auth_user.id == user.id).update(
            first_name=form.vars.first_name,
            last_name=form.vars.last_name,
            email=form.vars.email,
            timezone=form.vars.timezone,
            updated_on=datetime.utcnow()
        )
        db.commit()
        
        # Log profile update
        db.system_logs.insert(
            level='INFO',
            component='auth',
            message=f'User {user.username} updated profile',
            metadata=json.dumps({'user_id': user.id})
        )
        
        session.flash = 'Profile updated successfully'
        redirect(URL('auth/profile'))
    
    if password_form.accepted:
        current_password = password_form.vars.current_password
        new_password = password_form.vars.new_password
        confirm_password = password_form.vars.confirm_password
        
        if not verify_password(current_password, user.password_hash):
            password_form.errors['current_password'] = 'Current password is incorrect'
        elif new_password != confirm_password:
            password_form.errors['confirm_password'] = 'Passwords do not match'
        else:
            # Update password
            password_hash = hash_password(new_password)
            db(db.auth_user.id == user.id).update(
                password_hash=password_hash,
                password_changed_on=datetime.utcnow(),
                updated_on=datetime.utcnow()
            )
            db.commit()
            
            # Log password change
            db.system_logs.insert(
                level='INFO',
                component='auth',
                message=f'User {user.username} changed password',
                metadata=json.dumps({'user_id': user.id})
            )
            
            session.flash = 'Password changed successfully'
            redirect(URL('auth/profile'))
    
    # Get recent login attempts
    recent_logins = db(db.auth_login_attempts.username == user.username).select(
        orderby=~db.auth_login_attempts.attempted_on,
        limitby=(0, 10)
    )
    
    return {
        'form': form,
        'password_form': password_form,
        'user': user,
        'recent_logins': recent_logins
    }

@action("admin/users")
@action.uses("admin/users.html", auth.user, db)
def admin_users():
    """User management page (admin only)"""
    
    if not is_admin(auth.current_user):
        abort(403)
    
    # Get all users
    users = db(db.auth_user).select(
        orderby=[~db.auth_user.is_active, db.auth_user.username]
    )
    
    return {'users': users}

@action("admin/user/create")
@action("admin/user/edit/<user_id:int>")
@action.uses("admin/user_form.html", auth.user, db)
def admin_user_form(user_id=None):
    """Create or edit user (admin only)"""
    
    if not is_admin(auth.current_user):
        abort(403)
    
    user = None
    if user_id:
        user = db(db.auth_user.id == user_id).select().first()
        if not user:
            abort(404)
    
    # Create form
    form_fields = [
        Field('username', 'string', length=255,
              requires=[IS_NOT_EMPTY(), IS_LENGTH(3, 255)],
              default=user.username if user else ''),
        Field('email', 'string', length=255,
              requires=IS_EMAIL(),
              default=user.email if user else ''),
        Field('first_name', 'string', length=255,
              default=user.first_name if user else ''),
        Field('last_name', 'string', length=255,
              default=user.last_name if user else ''),
        Field('role', 'string', length=50,
              requires=IS_IN_SET(['admin', 'operator', 'viewer']),
              default=user.role if user else 'viewer'),
        Field('is_active', 'boolean',
              default=user.is_active if user else True)
    ]
    
    # Add password field for new users
    if not user:
        form_fields.insert(2, Field('password', 'password',
                                  requires=[IS_NOT_EMPTY(), IS_LENGTH(8, 255)]))
        form_fields.insert(3, Field('confirm_password', 'password',
                                  requires=IS_NOT_EMPTY()))
    
    form = Form(form_fields, formstyle=FormStyleBootstrap4)
    
    if form.accepted:
        try:
            # Validate username uniqueness
            existing_user = db((db.auth_user.username == form.vars.username) & 
                              (db.auth_user.id != user_id if user_id else True)).select().first()
            
            if existing_user:
                form.errors['username'] = 'Username already exists'
            elif not user and form.vars.password != form.vars.confirm_password:
                form.errors['confirm_password'] = 'Passwords do not match'
            else:
                data = {
                    'username': form.vars.username,
                    'email': form.vars.email,
                    'first_name': form.vars.first_name,
                    'last_name': form.vars.last_name,
                    'role': form.vars.role,
                    'is_active': form.vars.is_active,
                    'updated_on': datetime.utcnow()
                }
                
                if user:
                    # Update existing user
                    db(db.auth_user.id == user_id).update(**data)
                    logger.info(f"Updated user: {form.vars.username}")
                    
                    # Log user update
                    db.system_logs.insert(
                        level='INFO',
                        component='user_management',
                        message=f'User {form.vars.username} updated by {auth.current_user.username}',
                        metadata=json.dumps({
                            'user_id': user_id,
                            'updated_by': auth.current_user.id
                        })
                    )
                else:
                    # Create new user
                    data.update({
                        'password_hash': hash_password(form.vars.password),
                        'created_on': datetime.utcnow(),
                        'password_changed_on': datetime.utcnow()
                    })
                    
                    new_user_id = db.auth_user.insert(**data)
                    logger.info(f"Created user: {form.vars.username}")
                    
                    # Log user creation
                    db.system_logs.insert(
                        level='INFO',
                        component='user_management',
                        message=f'User {form.vars.username} created by {auth.current_user.username}',
                        metadata=json.dumps({
                            'user_id': new_user_id,
                            'created_by': auth.current_user.id
                        })
                    )
                
                db.commit()
                redirect(URL('admin/users'))
                
        except Exception as e:
            logger.error(f"Failed to save user: {e}")
            form.errors['general'] = f'Failed to save user: {str(e)}'
    
    return {
        'form': form,
        'user': user,
        'is_edit': user is not None
    }

@action("admin/user/delete/<user_id:int>")
@action.uses(auth.user, db)
def admin_user_delete(user_id):
    """Delete user (admin only)"""
    
    if not is_admin(auth.current_user):
        response.status = 403
        return {'success': False, 'error': 'Access denied'}
    
    user = db(db.auth_user.id == user_id).select().first()
    if not user:
        response.status = 404
        return {'success': False, 'error': 'User not found'}
    
    if user.id == auth.current_user.id:
        response.status = 400
        return {'success': False, 'error': 'Cannot delete your own account'}
    
    try:
        # Delete user
        db(db.auth_user.id == user_id).delete()
        
        # Log user deletion
        db.system_logs.insert(
            level='WARNING',
            component='user_management',
            message=f'User {user.username} deleted by {auth.current_user.username}',
            metadata=json.dumps({
                'deleted_user_id': user_id,
                'deleted_username': user.username,
                'deleted_by': auth.current_user.id
            })
        )
        
        db.commit()
        
        logger.info(f"Deleted user: {user.username}")
        
        return {'success': True, 'message': 'User deleted successfully'}
        
    except Exception as e:
        logger.error(f"Failed to delete user: {e}")
        response.status = 500
        return {'success': False, 'error': str(e)}

def hash_password(password):
    """Hash a password using PBKDF2 with SHA256"""
    import hashlib
    import secrets
    
    # Generate a random salt
    salt = secrets.token_bytes(32)
    
    # Hash the password
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    
    # Return salt + hash as base64
    import base64
    return base64.b64encode(salt + pwd_hash).decode('ascii')

def verify_password(password, password_hash):
    """Verify a password against its hash"""
    import hashlib
    import base64
    
    try:
        # Decode the stored password hash
        stored_hash = base64.b64decode(password_hash.encode('ascii'))
        
        # Extract salt (first 32 bytes) and hash (remaining bytes)
        salt = stored_hash[:32]
        stored_pwd_hash = stored_hash[32:]
        
        # Hash the provided password with the same salt
        pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        
        # Compare hashes
        return pwd_hash == stored_pwd_hash
    except:
        return False

def generate_remember_token(user_id):
    """Generate a remember me token"""
    import secrets
    token = secrets.token_urlsafe(32)
    
    # Store token in database (you might want to add this table)
    # For now, just return the token
    return token

def is_admin(user):
    """Check if user has admin privileges"""
    return user and user.role == 'admin'

def is_operator(user):
    """Check if user has operator privileges"""
    return user and user.role in ['admin', 'operator']

# Authentication middleware
@action.uses(auth.user)
def require_auth():
    """Require authentication for protected routes"""
    pass

@action.uses(auth.user)
def require_admin():
    """Require admin role for admin routes"""
    if not is_admin(auth.current_user):
        abort(403)

@action.uses(auth.user)
def require_operator():
    """Require operator role for operator routes"""
    if not is_operator(auth.current_user):
        abort(403)
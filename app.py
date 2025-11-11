from flask import Flask, render_template, request, send_from_directory, redirect, session, g, url_for, make_response, flash
from models import db, Question, BroadTheme, SpecificTheme, User, Country, ImageAsset, AnswerImageLink, QuizRuleSet, UserQuestionStat, UserQuizSession, QuestionAnswerStat, Profile, Conversation, ConversationParticipant, ConversationMessage, QuestionReport, ContactMessage, Keyword
from datetime import datetime
import random
import os
import re
import json
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func, text, or_
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import io
try:
    from PIL import Image
except Exception:
    Image = None
from unidecode import unidecode
from email_utils import send_email_optional
from config import config

app = Flask(__name__)

# Configuration selon l'environnement
config_name = os.environ.get('FLASK_ENV') or 'development'
app.config.from_object(config[config_name])
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB
app.config['SOUNDS_FOLDER'] = os.path.join(os.getcwd(), 'ressources', 'sounds')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

# Créer les tables
with app.app_context():
    db.create_all()
    # Auto-migration légère pour SQLite: ajout des nouvelles colonnes de users si manquantes
    try:
        if db.engine.url.drivername.startswith('sqlite'):
            result = db.session.execute(text("PRAGMA table_info(users)"))
            existing_cols = {row[1] for row in result.fetchall()}
            # password_hash
            if 'password_hash' not in existing_cols:
                db.session.execute(text("ALTER TABLE users ADD COLUMN password_hash TEXT"))
            # is_admin (0/1)
            if 'is_admin' not in existing_cols:
                db.session.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            # preferences_json
            if 'preferences_json' not in existing_cols:
                db.session.execute(text("ALTER TABLE users ADD COLUMN preferences_json TEXT"))
            # profile_id (nullable)
            if 'profile_id' not in existing_cols:
                db.session.execute(text("ALTER TABLE users ADD COLUMN profile_id INTEGER"))
            db.session.commit()

            # Migration pour la table questions
            result_questions = db.session.execute(text("PRAGMA table_info(questions)"))
            existing_cols_questions = {row[1] for row in result_questions.fetchall()}
            # is_private (False par défaut = publique)
            if 'is_private' not in existing_cols_questions:
                db.session.execute(text("ALTER TABLE questions ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT 0"))
            db.session.commit()
    except Exception:
        # Ne bloque pas l'app; pour autres SGBD, utiliser une migration Alembic
        db.session.rollback()

    # Seed de profils par défaut (idempotent)
    try:
        def ensure_profile(name: str, **perms):
            p = Profile.query.filter_by(name=name).first()
            if not p:
                p = Profile(name=name, **perms)
                db.session.add(p)
            else:
                # Mettre à jour si de nouveaux flags ajoutés
                for k, v in perms.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
            return p

        # Administrateur: tous droits
        ensure_profile(
            'Administrateur',
            description="Accès complet à l'administration",
            can_access_admin=True,
            can_create_question=True,
            can_update_delete_own_question=True,
            can_update_delete_any_question=True,
            can_create_rule=True,
            can_update_delete_own_rule=True,
            can_update_delete_any_rule=True,
            can_manage_users=True,
            can_manage_profiles=True,
        )

        # Éditeur: gère ses contenus, accès admin
        ensure_profile(
            'Éditeur',
            description="Peut gérer ses questions et ses règles",
            can_access_admin=True,
            can_create_question=True,
            can_update_delete_own_question=True,
            can_update_delete_any_question=False,
            can_create_rule=True,
            can_update_delete_own_rule=True,
            can_update_delete_any_rule=False,
            can_manage_users=False,
            can_manage_profiles=False,
        )

        # Modérateur: peut modifier/supprimer globalement, mais ne gère pas utilisateurs/profils
        ensure_profile(
            'Modérateur',
            description="Peut modérer toutes les questions et règles",
            can_access_admin=True,
            can_create_question=False,
            can_update_delete_own_question=True,
            can_update_delete_any_question=True,
            can_create_rule=False,
            can_update_delete_own_rule=True,
            can_update_delete_any_rule=True,
            can_manage_users=False,
            can_manage_profiles=False,
        )

        # Lecteur: accès admin en lecture (listes), pas de création ni modification
        ensure_profile(
            'Lecteur',
            description="Accès en lecture seule à l'administration",
            can_access_admin=False,  # Pas d'accès admin pour les lecteurs
            can_create_question=False,
            can_update_delete_own_question=False,
            can_update_delete_any_question=False,
            can_create_rule=False,
            can_update_delete_own_rule=False,
            can_update_delete_any_rule=False,
            can_manage_users=False,
            can_manage_profiles=False,
        )

        db.session.commit()

        # Créer un administrateur par défaut si aucun admin n'existe
        admin_profile = Profile.query.filter_by(name='Administrateur').first()
        if admin_profile:
            admin_count = User.query.filter_by(profile_id=admin_profile.id, is_active=True).count()
            if admin_count == 0:
                # Créer l'admin par défaut
                from werkzeug.security import generate_password_hash
                default_admin = User(
                    username='admin',
                    email='admin@geocaching-quiz.com',
                    password_hash=generate_password_hash('admin123'),
                    is_active=True,
                    profile_id=admin_profile.id
                )
                db.session.add(default_admin)
                db.session.commit()
                print("[INIT] Administrateur par défaut créé: username='admin', password='admin123'")

    except Exception as e:
        db.session.rollback()
        print(f"[WARN] Erreur lors de l'initialisation des données: {e}")

# ================== Gestion Session / Utilisateur ==================

@app.before_request
def load_current_user():
    user_id = session.get('user_id')
    g.current_user = db.session.get(User, user_id) if user_id else None


@app.context_processor
def inject_current_user():
    return { 'current_user': getattr(g, 'current_user', None) }




# ================== Helpers Permissions ==================

def _has_perm(perm_attr: str) -> bool:
    user = getattr(g, 'current_user', None)
    return bool(user and user.has_perm(perm_attr))


@app.route('/access-denied')
def access_denied_page():
    """Page d'explication d'accès refusé."""
    user = getattr(g, 'current_user', None)
    return render_template('access_denied_full.html', current_user=user)


def _ensure_admin_page_redirect():
    """Pour les pages complètes: redirige si pas d'accès admin."""
    if not _has_perm('can_access_admin'):
        return redirect(url_for('access_denied_page'))
    return None


def _ensure_perm_api(*perm_attrs: str):
    """Pour endpoints HTMX/API: renvoie (template_html, 200) si refusé, sinon None.
    Toutes les permissions listées doivent être vraies (ET logique).
    HTMX traite mieux les 200 avec contenu HTML qu'un 403.
    """
    user = getattr(g, 'current_user', None)
    if not _has_perm('can_access_admin'):
        return (render_template('access_denied.html', reason="Accès à l'administration requis", current_user=user), 200)
    for p in perm_attrs:
        if not _has_perm(p):
            return (render_template('access_denied.html', reason=f"Permission '{p}' requise", current_user=user), 200)
    return None


def _deny_access(reason: str):
    """Retourne un template d'accès refusé avec la raison spécifiée."""
    user = getattr(g, 'current_user', None)
    return render_template('access_denied.html', reason=reason, current_user=user), 200


@app.route('/auth/widget')
def auth_widget():
    # Calculer le nombre de messages non lus pour l'utilisateur connecté
    unread = 0
    user = getattr(g, 'current_user', None)
    if user and user.password_hash:
        try:
            parts = ConversationParticipant.query.filter_by(user_id=user.id).all()
            print(f"[WIDGET] User {user.username} has {len(parts)} conversation participations")
            for p in parts:
                last_read = p.last_read_at or datetime.min
                # Pour les nouveaux participants (last_read_at=None), compter tous les messages sauf ceux de l'utilisateur
                if p.last_read_at is None:
                    count = ConversationMessage.query.filter(
                        ConversationMessage.conversation_id == p.conversation_id,
                        or_(ConversationMessage.sender_id.is_(None), ConversationMessage.sender_id != user.id)
                    ).count()
                    print(f"[WIDGET] Conversation {p.conversation_id}: NEW participant, messages={count}")
                else:
                    count = ConversationMessage.query.filter(
                        ConversationMessage.conversation_id == p.conversation_id,
                        ConversationMessage.created_at > last_read,
                        or_(ConversationMessage.sender_id.is_(None), ConversationMessage.sender_id != user.id)
                    ).count()
                    print(f"[WIDGET] Conversation {p.conversation_id}: last_read={p.last_read_at}, messages={count}")
                unread += count
            print(f"[WIDGET] Total unread for {user.username}: {unread}")
        except Exception as e:
            print(f"[WIDGET] Error calculating unread: {e}")
            unread = 0
    return render_template('auth_widget.html', unread_count=unread)


@app.route('/auth/quick-login', methods=['POST'])
def quick_login():
    pseudo = (request.form.get('pseudo') or '').strip()
    if not pseudo:
        return "Pseudo requis", 400

    next_url = (request.form.get('next') or request.headers.get('HX-Redirect') or url_for('play_quiz'))
    source = (request.form.get('source') or 'widget')

    # Chercher utilisateur par username exact
    user = User.query.filter_by(username=pseudo).first()

    if not user:
        # Créer un user sans mot de passe (joueur standard)
        user = User(username=pseudo, email=None, is_active=True)
        db.session.add(user)
        db.session.commit()
        session['user_id'] = user.id
        # Assurer que le widget reflète l'état connecté dans cette même réponse
        g.current_user = user
        resp = make_response('')
        resp.headers['HX-Redirect'] = next_url
        resp.headers['HX-Trigger'] = json.dumps({'quiz-login-success': {'source': source, 'username': user.username}})
        return resp
    elif user.password_hash:
        # Si l'utilisateur a un mot de passe, afficher le formulaire de connexion avec pseudo pré-rempli
        if source == 'play-start':
            resp = make_response('')
            resp.headers['HX-Trigger'] = json.dumps({'quiz-login-password-required': {'username': pseudo, 'next': next_url}})
            return resp
        return render_template(
            'auth_widget.html',
            login_username=pseudo,
            show_password_form=True,
            next_url=next_url,
            source=source,
        )
    else:
        # Utilisateur existant sans mot de passe, connexion directe
        session['user_id'] = user.id
        # Assurer que le widget reflète l'état connecté dans cette même réponse
        g.current_user = user
        resp = make_response('')
        resp.headers['HX-Redirect'] = next_url
        resp.headers['HX-Trigger'] = json.dumps({'quiz-login-success': {'source': source, 'username': user.username}})
        return resp


@app.route('/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    # Assurer que le widget reflète l'état déconnecté dans cette même réponse
    g.current_user = None
    resp = make_response('')
    resp.headers['HX-Redirect'] = url_for('index')
    return resp


@app.route('/auth/widget-login', methods=['POST'])
def widget_login():
    """Connexion depuis le widget avec pseudo + mot de passe."""
    username = (request.form.get('username') or '').strip()
    password = (request.form.get('password') or '').strip()
    next_url = (request.form.get('next') or request.headers.get('HX-Redirect') or url_for('play_quiz'))
    source = (request.form.get('source') or 'widget')

    if not username or not password:
        if source == 'play-start':
            resp = make_response('')
            resp.headers['HX-Trigger'] = json.dumps({'quiz-login-password-error': {'message': "Pseudo et mot de passe requis"}})
            return resp
        return render_template(
            'auth_widget.html',
            login_username=username,
            show_password_form=True,
            error_message="Pseudo et mot de passe requis",
            next_url=next_url,
            source=source,
        )

    user = User.query.filter_by(username=username).first()
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        if source == 'play-start':
            resp = make_response('')
            resp.headers['HX-Trigger'] = json.dumps({'quiz-login-password-error': {'message': "Identifiants invalides"}})
            return resp
        return render_template(
            'auth_widget.html',
            login_username=username,
            show_password_form=True,
            error_message="Identifiants invalides",
            next_url=next_url,
            source=source,
        )

    session['user_id'] = user.id
    # Assurer que le widget reflète l'état connecté dans cette même réponse
    g.current_user = user
    resp = make_response('')
    resp.headers['HX-Redirect'] = next_url
    resp.headers['HX-Trigger'] = json.dumps({'quiz-login-success': {'source': source, 'username': user.username}})
    return resp


@app.route('/auth/upgrade-account', methods=['POST'])
def upgrade_account():
    """Permet à un utilisateur connecté sans mot de passe d'ajouter email/mot de passe."""
    if not getattr(g, 'current_user', None):
        return "<div class='alert alert-danger'>Vous devez être connecté pour effectuer cette action.</div>", 403

    user = g.current_user
    if user.password_hash:
        return "<div class='alert alert-warning'>Votre compte est déjà sécurisé avec un mot de passe.</div>"

    email = (request.form.get('email') or '').strip()
    password = request.form.get('password', '').strip()
    password_confirm = request.form.get('password_confirm', '').strip()

    errors = []

    # Validation email (optionnel)
    if email and not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
        errors.append("Format d'email invalide")

    # Validation mot de passe
    if not password:
        errors.append("Le mot de passe est requis")
    elif len(password) < 6:
        errors.append("Le mot de passe doit contenir au moins 6 caractères")
    elif password != password_confirm:
        errors.append("Les mots de passe ne correspondent pas")

    if errors:
        error_html = "<div class='alert alert-danger'><ul>"
        for error in errors:
            error_html += f"<li>{error}</li>"
        error_html += "</ul></div>"
        return error_html

    # Mettre à jour l'utilisateur
    user.email = email if email else None
    user.password_hash = generate_password_hash(password)
    db.session.commit()

    # Fermer la modal et afficher un message de succès
    return """
    <div class='success-message'>
        <div style='text-align: center; padding: 2rem;'>
            <h3 style='color: var(--success-color); margin-bottom: 1rem;'>✅ Compte sécurisé !</h3>
            <p>Votre compte est maintenant protégé par un mot de passe.</p>
            <p>Vous pouvez accéder à vos statistiques détaillées et votre progression est sauvegardée.</p>
            <button type='button' class='btn btn-primary' onclick='hideUpgradeModal(); location.reload();' style='margin-top: 1rem;'>
                Continuer à jouer
            </button>
        </div>
    </div>
    """


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        if not username or not password:
            return render_template('login.html', error="Identifiants requis")
        user = User.query.filter_by(username=username).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            return render_template('login.html', error="Identifiants invalides")
        session['user_id'] = user.id
        next_url = request.args.get('next') or url_for('play_quiz')
        return redirect(next_url)
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip() or None
        # display_name supprimé, on utilise directement le username
        password = (request.form.get('password') or '').strip()
        password2 = (request.form.get('password2') or '').strip()
        if not username or not password:
            return render_template('register.html', error="Nom d'utilisateur et mot de passe requis")
        if password != password2:
            return render_template('register.html', error="Les mots de passe ne correspondent pas")
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error="Ce nom d'utilisateur est déjà pris")
        # Créer l'utilisateur avec mot de passe hashé
        user = User(
            username=username,
            email=email,
            is_active=True,
            password_hash=generate_password_hash(password)
        )
        db.session.add(user)
        db.session.commit()
        session['user_id'] = user.id
        return redirect(url_for('play_quiz'))
    return render_template('register.html')


def _get_token_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='password-reset')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        if not email:
            return render_template('forgot_password.html', error="Email requis")
        user = User.query.filter_by(email=email).first()
        # Toujours indiquer que l'email a été envoyé pour éviter la fuite d'existence
        if user:
            s = _get_token_serializer()
            token = s.dumps({'uid': user.id})
            # Ici on simule l'envoi: on rend la page avec le lien (POC). En prod, envoyer un email.
            reset_link = url_for('reset_password', token=token, _external=True)
            return render_template('forgot_password.html', info="Un email a été envoyé.", reset_link=reset_link)
        return render_template('forgot_password.html', info="Un email a été envoyé.")
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    s = _get_token_serializer()
    try:
        data = s.loads(token, max_age=3600)  # 1h
        user_id = data.get('uid')
    except SignatureExpired:
        return render_template('reset_password.html', error="Lien expiré"), 400
    except BadSignature:
        return render_template('reset_password.html', error="Lien invalide"), 400

    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        password2 = (request.form.get('password2') or '').strip()
        if not password:
            return render_template('reset_password.html', error="Mot de passe requis", token=token)
        if password != password2:
            return render_template('reset_password.html', error="Les mots de passe ne correspondent pas", token=token)
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        return redirect(url_for('login_page'))
    return render_template('reset_password.html', token=token)


@app.route('/me')
def me_page():
    if not g.current_user:
        return redirect(url_for('play_quiz'))
    # Dernières 20 réponses
    stats = (UserQuestionStat.query
             .filter_by(user_id=g.current_user.id)
             .order_by(UserQuestionStat.last_answered_at.desc())
             .limit(20)
             .all())
    # Totaux globaux
    totals = (db.session.query(
                    func.coalesce(func.sum(UserQuestionStat.times_answered), 0),
                    func.coalesce(func.sum(UserQuestionStat.success_count), 0)
               )
               .filter(UserQuestionStat.user_id == g.current_user.id)
               .one())
    total_answers = totals[0] or 0
    total_success = totals[1] or 0
    # Agrégats par thème large
    agg_broad_rows = (db.session.query(
                        Question.broad_theme_id,
                        BroadTheme.name,
                        func.coalesce(func.sum(UserQuestionStat.times_answered), 0),
                        func.coalesce(func.sum(UserQuestionStat.success_count), 0)
                      )
                      .join(Question, Question.id == UserQuestionStat.question_id)
                      .outerjoin(BroadTheme, BroadTheme.id == Question.broad_theme_id)
                      .filter(UserQuestionStat.user_id == g.current_user.id)
                      .group_by(Question.broad_theme_id, BroadTheme.name)
                      .order_by(func.coalesce(func.sum(UserQuestionStat.times_answered), 0).desc())
                      .all())
    agg_by_broad = [
        {
            'theme_id': row[0],
            'theme_name': row[1] or 'Sans thème',
            'answered': int(row[2] or 0),
            'success': int(row[3] or 0),
            'rate': (float(row[3]) / float(row[2]) * 100.0) if (row[2] or 0) > 0 else 0.0,
        }
        for row in agg_broad_rows
    ]
    # Agrégats par thème spécifique
    agg_spec_rows = (db.session.query(
                        Question.specific_theme_id,
                        SpecificTheme.name,
                        func.coalesce(func.sum(UserQuestionStat.times_answered), 0),
                        func.coalesce(func.sum(UserQuestionStat.success_count), 0)
                      )
                      .join(Question, Question.id == UserQuestionStat.question_id)
                      .outerjoin(SpecificTheme, SpecificTheme.id == Question.specific_theme_id)
                      .filter(UserQuestionStat.user_id == g.current_user.id)
                      .group_by(Question.specific_theme_id, SpecificTheme.name)
                      .order_by(func.coalesce(func.sum(UserQuestionStat.times_answered), 0).desc())
                      .all())
    agg_by_specific = [
        {
            'specific_theme_id': row[0],
            'specific_theme_name': row[1] or 'Sans sous-thème',
            'answered': int(row[2] or 0),
            'success': int(row[3] or 0),
            'rate': (float(row[3]) / float(row[2]) * 100.0) if (row[2] or 0) > 0 else 0.0,
        }
        for row in agg_spec_rows
    ]
    # Agrégats par difficulté
    agg_diff_rows = (db.session.query(
                        Question.difficulty_level,
                        func.coalesce(func.sum(UserQuestionStat.times_answered), 0),
                        func.coalesce(func.sum(UserQuestionStat.success_count), 0)
                      )
                      .join(Question, Question.id == UserQuestionStat.question_id)
                      .filter(UserQuestionStat.user_id == g.current_user.id)
                      .group_by(Question.difficulty_level)
                      .order_by(Question.difficulty_level)
                      .all())
    agg_by_difficulty = [
        {
            'difficulty': row[0],
            'answered': int(row[1] or 0),
            'success': int(row[2] or 0),
            'rate': (float(row[2]) / float(row[1]) * 100.0) if (row[1] or 0) > 0 else 0.0,
        }
        for row in agg_diff_rows
    ]
    # Compteurs de sessions
    sessions_completed = 0
    sessions_abandoned = 0
    if getattr(g, 'current_user', None):
        sessions_completed = (UserQuizSession.query
                              .filter_by(user_id=g.current_user.id, status='completed')
                              .count())
        sessions_abandoned = (UserQuizSession.query
                              .filter_by(user_id=g.current_user.id, status='abandoned')
                              .count())

    return render_template('me.html',
                           stats=stats,
                           total_answers=total_answers,
                           total_success=total_success,
                           agg_by_broad=agg_by_broad,
                           agg_by_specific=agg_by_specific,
                           agg_by_difficulty=agg_by_difficulty,
                           sessions_completed=sessions_completed,
                           sessions_abandoned=sessions_abandoned)


@app.route('/preferences', methods=['GET', 'POST'])
def preferences():
    if not g.current_user:
        return redirect(url_for('play_quiz'))

    # Seuls les utilisateurs avec mot de passe peuvent accéder aux préférences
    if not g.current_user.password_hash:
        flash("Cette page n'est accessible qu'aux utilisateurs enregistrés.", "warning")
        return redirect(url_for('play_quiz'))

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()

        # Validation basique de l'email
        if email and not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            flash("Adresse email invalide.", "danger")
            return render_template('preferences.html', user=g.current_user)

        # Mettre à jour l'email
        g.current_user.email = email

        # Traiter les préférences de jeu et de notification
        prefs = g.current_user.get_preferences()
        prefs['double_click_validation'] = (request.form.get('double_click_validation') == '1')
        prefs['notify_email_on_message'] = (request.form.get('notify_email_on_message') == '1')
        g.current_user.set_preferences(prefs)

        db.session.commit()
        flash("Préférences mises à jour avec succès.", "success")
        return redirect(url_for('preferences'))

    return render_template('preferences.html', user=g.current_user)


@app.route('/delete-account', methods=['POST'])
def delete_account():
    """Supprime définitivement le compte utilisateur et toutes ses données."""
    if not g.current_user:
        return redirect(url_for('play_quiz'))

    # Seuls les utilisateurs avec mot de passe peuvent supprimer leur compte
    if not g.current_user.password_hash:
        flash("Cette action n'est disponible que pour les utilisateurs enregistrés.", "warning")
        return redirect(url_for('preferences'))

    user_id = g.current_user.id
    username = g.current_user.username

    try:
        # Supprimer explicitement les données liées pour s'assurer qu'elles sont supprimées
        UserQuestionStat.query.filter_by(user_id=user_id).delete()
        UserQuizSession.query.filter_by(user_id=user_id).delete()

        # Supprimer l'utilisateur (les foreign keys avec cascade s'occuperont du reste)
        db.session.delete(g.current_user)
        db.session.commit()

        # Nettoyer la session
        session.clear()

        flash(f"Le compte de {username} a été supprimé définitivement.", "success")
        return redirect(url_for('index'))

    except Exception as e:
        db.session.rollback()
        flash("Une erreur est survenue lors de la suppression du compte. Veuillez réessayer.", "danger")
        return redirect(url_for('preferences'))


# ================== Fichiers uploadés (serveur) ==================

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ================== Fichiers sons ==================
@app.route('/sounds/<path:filename>')
def sounds_file(filename):
    # Sert les fichiers audio depuis ressources/sounds
    return send_from_directory(app.config['SOUNDS_FOLDER'], filename)


# ================== Gestion des Images ==================

@app.route('/images')
def images_page():
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('images.html')


@app.route('/api/images')
def list_images_api():
    denied = _ensure_perm_api()
    if denied:
        return denied
    search = request.args.get('search', '').strip()
    selected_id = request.args.get('selected_id', type=int)
    query = ImageAsset.query
    if search:
        like = f"%{search}%"
        try:
            from sqlalchemy import or_
            query = query.filter(
                or_(
                    ImageAsset.title.like(like),
                    ImageAsset.filename.like(like),
                    ImageAsset.alt_text.like(like)
                )
            )
        except Exception:
            # Fallback: filtre sur le titre uniquement
            query = query.filter(ImageAsset.title.like(like))
    images = query.order_by(ImageAsset.created_at.desc()).all()
    if selected_id:
        images.sort(key=lambda img: 0 if img.id == selected_id else 1)
    return render_template('images_list.html', images=images)


@app.route('/api/images/json')
def list_images_json():
    """Retourne la liste des images au format JSON pour les selects"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    search = request.args.get('search', '').strip()
    selected_id = request.args.get('selected_id', type=int)
    query = ImageAsset.query
    if search:
        like = f"%{search}%"
        try:
            from sqlalchemy import or_
            query = query.filter(
                or_(
                    ImageAsset.title.like(like),
                    ImageAsset.filename.like(like),
                    ImageAsset.alt_text.like(like)
                )
            )
        except Exception:
            query = query.filter(ImageAsset.title.like(like))
    images = query.order_by(ImageAsset.title).all()
    if selected_id:
        images.sort(key=lambda img: 0 if img.id == selected_id else 1)
    return [{
        'id': img.id,
        'title': img.title,
        'filename': img.filename,
        'alt_text': img.alt_text
    } for img in images]

@app.route('/api/images/gallery')
def images_gallery_fragment():
    denied = _ensure_perm_api()
    if denied:
        return denied
    search = request.args.get('search', '').strip()
    selected_id = request.args.get('selected_id', type=int)
    select_id = request.args.get('select_id', '')
    partial = request.args.get('partial', '0') == '1'
    print(f"[DEBUG] /api/images/gallery called with search='{search}', selected_id={selected_id}, select_id='{select_id}', partial={partial}")
    query = ImageAsset.query
    if search:
        like = f"%{search}%"
        print(f"[DEBUG] Filtering with search pattern: {like}")
        try:
            from sqlalchemy import or_
            query = query.filter(
                or_(
                    ImageAsset.title.like(like),
                    ImageAsset.filename.like(like),
                    ImageAsset.alt_text.like(like)
                )
            )
        except Exception:
            query = query.filter(ImageAsset.title.like(like))
    images = query.order_by(ImageAsset.created_at.desc()).all()
    print(f"[DEBUG] Found {len(images)} images after filtering")
    if selected_id:
        images.sort(key=lambda img: 0 if img.id == selected_id else 1)

    if partial:
        # Retourner seulement la grille d'images pour les mises à jour partielles
        return render_template('images_gallery_grid.html', images=images, selected_id=selected_id or 0, select_id=select_id)
    else:
        # Retourner le HTML complet pour l'ouverture initiale
        return render_template('images_gallery.html', images=images, selected_id=selected_id or 0, select_id=select_id)


@app.route('/image/new')
def new_image():
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    embedded = request.args.get('embedded') in ('1', 'true', 'yes')
    select_id = request.args.get('select_id') or ''
    return render_template('image_form.html', image=None, embedded=embedded, select_id=select_id)


@app.route('/image/<int:image_id>/edit')
def edit_image(image_id: int):
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    image = ImageAsset.query.get_or_404(image_id)
    embedded = request.args.get('embedded') in ('1', 'true', 'yes')
    select_id = request.args.get('select_id') or ''
    return render_template('image_form.html', image=image, embedded=embedded, select_id=select_id)


def _secure_filename(original_name: str) -> str:
    # Sécuriser le nom de fichier simplement (remplacer espaces, enlever caractères spéciaux)
    base = os.path.basename(original_name)
    base = base.replace(' ', '_')
    keep = "-_.()abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    safe = ''.join(ch for ch in base if ch in keep)
    if not safe:
        safe = f'image_{int(datetime.utcnow().timestamp())}.bin'
    return safe

def _optimize_image(file_storage, base_name: str):
    """Optimise l'image pour le web (resize + WebP). Retourne (bytes, new_ext, mime) ou (None, None, None)."""
    if Image is None or file_storage is None:
        return None, None, None
    try:
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
        # GIF animé: on ne touche pas
        if bool(getattr(img, 'is_animated', False)):
            return None, None, None

        has_alpha = (img.mode in ('RGBA', 'LA') or 'transparency' in img.info)
        img = img.convert('RGBA') if has_alpha else img.convert('RGB')

        # Redimension max 1600px
        max_size = (1600, 1600)
        img.thumbnail(max_size, Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format='WEBP', quality=80, method=6)
        data = out.getvalue()
        return data, '.webp', 'image/webp'
    except Exception:
        return None, None, None


@app.route('/api/image', methods=['POST'])
def create_image():
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        title = request.form.get('title', '').strip()
        alt_text = request.form.get('alt_text', '').strip()
        copyright_credits = request.form.get('copyright_credits', '').strip()
        copyright_link = request.form.get('copyright_link', '').strip()
        file = request.files.get('file')
        if not title:
            return "Titre requis", 400
        if not file:
            return "Fichier requis", 400

        # Nom de base et optimisation
        original_secure = _secure_filename(file.filename)
        base_name, orig_ext = os.path.splitext(original_secure)

        optimized_bytes, new_ext, new_mime = _optimize_image(file, base_name)
        if optimized_bytes is not None:
            filename = f"{base_name}{new_ext}"
            # Unicité DB uniquement
            counter = 1
            while ImageAsset.query.filter_by(filename=filename).first() is not None:
                filename = f"{base_name}_{counter}{new_ext}"
                counter += 1
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            with open(filepath, 'wb') as f:
                f.write(optimized_bytes)
            size_bytes = len(optimized_bytes)
            mime_type = new_mime
        else:
            # Fallback: sauvegarde brute
            filename = original_secure
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.exists(filepath):
                name, ext = os.path.splitext(filename)
                filename = f"{name}_{int(datetime.utcnow().timestamp())}{ext}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            base_filename = filename
            counter = 1
            while ImageAsset.query.filter_by(filename=filename).first() is not None:
                name, ext = os.path.splitext(base_filename)
                filename = f"{name}_{counter}{ext}"
                counter += 1
            if filename != base_filename:
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            size_bytes = os.path.getsize(filepath)
            mime_type = file.mimetype

        image = ImageAsset(title=title, filename=filename, mime_type=mime_type, size_bytes=size_bytes, alt_text=alt_text, copyright_credits=copyright_credits, copyright_link=copyright_link)
        db.session.add(image)
        db.session.commit()

        # Si formulaire embarqué (modale au-dessus d'une autre modale): renvoyer JSON
        if request.form.get('embedded') in ('1', 'true', 'yes') or request.args.get('embedded') in ('1', 'true', 'yes'):
            return {
                'created_image': {
                    'id': image.id,
                    'title': image.title,
                    'filename': image.filename,
                    'alt_text': image.alt_text,
                    'copyright_credits': image.copyright_credits,
                    'copyright_link': image.copyright_link
                },
                'select_id': request.form.get('select_id') or request.args.get('select_id') or ''
            }

        images = ImageAsset.query.order_by(ImageAsset.created_at.desc()).all()
        return render_template('images_list.html', images=images)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/image/<int:image_id>', methods=['POST', 'PUT'])
def update_image(image_id: int):
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        image = ImageAsset.query.get_or_404(image_id)
        title = request.form.get('title', '').strip()
        alt_text = request.form.get('alt_text', '').strip()
        copyright_credits = request.form.get('copyright_credits', '').strip()
        copyright_link = request.form.get('copyright_link', '').strip()
        file = request.files.get('file')

        if title:
            image.title = title
        image.alt_text = alt_text
        image.copyright_credits = copyright_credits
        image.copyright_link = copyright_link

        if file:
            original_secure = _secure_filename(file.filename)
            base_name, orig_ext = os.path.splitext(original_secure)

            optimized_bytes, new_ext, new_mime = _optimize_image(file, base_name)
            if optimized_bytes is not None:
                filename = f"{base_name}{new_ext}"
                counter = 1
                while ImageAsset.query.filter_by(filename=filename).first() is not None and filename != image.filename:
                    filename = f"{base_name}_{counter}{new_ext}"
                    counter += 1
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                with open(filepath, 'wb') as f:
                    f.write(optimized_bytes)
                image.filename = filename
                image.mime_type = new_mime
                image.size_bytes = len(optimized_bytes)
                image.updated_at = datetime.utcnow()
            else:
                filename = original_secure
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if os.path.exists(filepath):
                    name, ext = os.path.splitext(filename)
                    filename = f"{name}_{int(datetime.utcnow().timestamp())}{ext}"
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                image.filename = filename
                image.mime_type = file.mimetype
                image.size_bytes = os.path.getsize(filepath)
                image.updated_at = datetime.utcnow()

        db.session.commit()
        images = ImageAsset.query.order_by(ImageAsset.created_at.desc()).all()
        return render_template('images_list.html', images=images)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/image/<int:image_id>', methods=['DELETE'])
def delete_image(image_id: int):
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        image = ImageAsset.query.get_or_404(image_id)
        # Empêcher la suppression si utilisée par des réponses ou questions
        if image.questions.count() > 0 or AnswerImageLink.query.filter_by(image_id=image.id).count() > 0:
            return "Impossible de supprimer: image utilisée.", 400

        # Supprimer le fichier physique si présent
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], image.filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

        db.session.delete(image)
        db.session.commit()

        images = ImageAsset.query.order_by(ImageAsset.created_at.desc()).all()
        return render_template('images_list.html', images=images)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/')
def index():
    """Accueil: page publique si non connecté, sinon page de jeu."""
    if getattr(g, 'current_user', None):
        return redirect(url_for('play_quiz'))
    return render_template('home_public.html')


@app.route('/contact', methods=['GET', 'POST'])
def contact_page():
    print(f"[CONTACT] Method: {request.method}")
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip()
        message = (request.form.get('message') or '').strip()
        print(f"[CONTACT] Received: name='{name}', email='{email}', message='{message[:50]}...'")

        if not name or not email or not message:
            print(f"[CONTACT] Validation failed: name={bool(name)}, email={bool(email)}, message={bool(message)}")
            flash('Tous les champs sont requis.', 'danger')
            return render_template('contact.html')

        try:
            print("[CONTACT] Creating ContactMessage...")
            # Créer le message de contact
            contact_msg = ContactMessage(
                visitor_name=name,
                visitor_email=email,
                message=message
            )
            db.session.add(contact_msg)
            db.session.flush()
            print(f"[CONTACT] ContactMessage created with id={contact_msg.id}")

            # Trouver les administrateurs (utilisateurs avec profil "Administrateur")
            print("[CONTACT] Looking for admin profile...")
            admin_profile = Profile.query.filter_by(name='Administrateur').first()
            admin_users = []
            if admin_profile:
                print(f"[CONTACT] Found admin profile id={admin_profile.id}")
                admin_users = User.query.filter_by(profile_id=admin_profile.id, is_active=True).all()
                print(f"[CONTACT] Found {len(admin_users)} active admin users: {[u.username for u in admin_users]}")
            else:
                print("[CONTACT] No admin profile found!")

            # Créer une conversation si il y a des admins
            if admin_users:
                print("[CONTACT] Creating conversation...")
                subject = f"Contact: Message de {name}"
                conv = Conversation(subject=subject, context_type='contact_message', context_id=contact_msg.id)
                db.session.add(conv)
                db.session.flush()
                print(f"[CONTACT] Conversation created with id={conv.id}")

                # Ajouter les participants (admins)
                for admin in admin_users:
                    print(f"[CONTACT] Adding participant: {admin.username} (id={admin.id})")
                    db.session.add(ConversationParticipant(conversation_id=conv.id, user_id=admin.id, last_read_at=None))

                # Message initial
                content = f"Message de contact de {name} ({email}):\n\n{message}"
                print("[CONTACT] Creating initial message...")
                msg = ConversationMessage(conversation_id=conv.id, sender_id=None, content=content)  # sender_id=None pour les messages système
                db.session.add(msg)

                # Lier la conversation au message de contact
                contact_msg.conversation_id = conv.id

                # Envoyer emails aux admins ayant activé les notifications
                for admin in admin_users:
                    prefs = admin.get_preferences()
                    notify = prefs.get('notify_email_on_message', False)
                    has_email = bool(admin.email)
                    print(f"[CONTACT] Admin {admin.username}: notify={notify}, has_email={has_email}")
                    if notify and has_email:
                        try:
                            send_email_optional(
                                to_email=admin.email,
                                subject=f"Nouveau message de contact: {subject}",
                                body=f"Un nouveau message de contact a été reçu de {name}.\n\n{message}\n\nAccéder à la conversation: {request.host_url.rstrip('/')}/messages"
                            )
                            print(f"[CONTACT] Email sent to {admin.email}")
                        except Exception as e:
                            print(f"[CONTACT] Email error for {admin.email}: {e}")

            print("[CONTACT] Committing transaction...")
            db.session.commit()
            print("[CONTACT] Transaction committed successfully")
            flash('Merci, votre message a été envoyé.', 'success')
            return redirect(url_for('contact_page'))

        except Exception as e:
            db.session.rollback()
            print(f"[CONTACT] Error during contact message creation: {e}")
            import traceback
            traceback.print_exc()
            flash('Une erreur est survenue lors de l\'envoi de votre message.', 'danger')
            return render_template('contact.html')

    return render_template('contact.html')


# ================== Signalement de problème sur une question ==================

@app.route('/api/report/form')
def report_form():
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "<div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-warning'>Vous devez être connecté avec un compte protégé par mot de passe pour signaler un problème.</div></div>", 200

    qid = (request.args.get('question_id') or '').strip()
    if not qid.isdigit():
        return "<div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Identifiant de question invalide.</div></div>", 200

    question = Question.query.get(int(qid))
    if not question:
        return "<div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Question introuvable.</div></div>", 200

    rule_set_slug = (request.args.get('rule_set') or '').strip()
    rule_set = None
    if rule_set_slug:
        rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()

    author_user = question.author_user
    rule_creator = rule_set.created_by_user if rule_set else None

    inner = render_template('report_form.html', question=question, rule_set=rule_set, author_user=author_user, rule_creator=rule_creator)
    # Remplacer entièrement le conteneur pour l'afficher
    return f"<div id='modal-root' class='modal-overlay' style='display:flex'>{inner}</div>"


@app.route('/api/report/submit', methods=['POST'])
def report_submit():
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "<div id='modal-root' class='modal-overlay' style='display:flex'><div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-warning'>Vous devez être connecté avec un compte protégé par mot de passe.</div></div></div>", 200

    qid = (request.form.get('question_id') or '').strip()
    reason = (request.form.get('reason') or '').strip()
    details = (request.form.get('details') or '').strip()
    rule_set_slug = (request.form.get('rule_set') or '').strip()

    if not qid.isdigit():
        return "<div id='modal-root' class='modal-overlay' style='display:flex'><div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Identifiant de question invalide.</div></div></div>", 200
    if not reason or not details:
        return "<div id='modal-root' class='modal-overlay' style='display:flex'><div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Merci de préciser la raison et les détails.</div></div></div>", 200

    question = Question.query.get(int(qid))
    if not question:
        return "<div id='modal-root' class='modal-overlay' style='display:flex'><div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Question introuvable.</div></div></div>", 200

    rule_set = None
    if rule_set_slug:
        rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()

    # Déterminer les destinataires
    to_author = (request.form.get('to_author') == '1')
    to_rule_creator = (request.form.get('to_rule_creator') == '1')
    to_admins = (request.form.get('to_admins') == '1')

    recipient_ids = set()
    if to_author and question.author_id:
        recipient_ids.add(int(question.author_id))
    if to_rule_creator and rule_set and rule_set.created_by_user_id:
        recipient_ids.add(int(rule_set.created_by_user_id))
    if to_admins:
        for adm in User.query.filter_by(is_admin=True, is_active=True).all():
            recipient_ids.add(adm.id)

    # Exclure l'expéditeur
    if user.id in recipient_ids:
        recipient_ids.remove(user.id)

    try:
        # Créer la conversation
        subject = f"Signalement Q{question.id}: {question.question_text[:60]}"
        conv = Conversation(subject=subject, context_type='question_report', context_id=None)
        db.session.add(conv)
        db.session.flush()

        # Participants: reporter + destinataires
        db.session.add(ConversationParticipant(conversation_id=conv.id, user_id=user.id, last_read_at=datetime.utcnow()))
        for rid in recipient_ids:
            db.session.add(ConversationParticipant(conversation_id=conv.id, user_id=rid, last_read_at=None))

        # Message initial
        content = f"Raison: {reason}\n\n{details}"
        msg = ConversationMessage(conversation_id=conv.id, sender_id=user.id, content=content)
        db.session.add(msg)
        db.session.flush()

        # Créer le report et relier la conversation
        report = QuestionReport(
            question_id=question.id,
            rule_set_id=(rule_set.id if rule_set else None),
            reporter_id=user.id,
            reason=reason,
            details=details,
            status='open',
            conversation_id=conv.id,
        )
        db.session.add(report)
        conv.context_id = report.id

        db.session.commit()

        # Envoi emails (optionnel)
        # Récupérer préférences des destinataires
        if recipient_ids:
            recips = User.query.filter(User.id.in_(list(recipient_ids))).all()
            for r in recips:
                prefs = r.get_preferences()
                if prefs.get('notify_email_on_message') and r.email:
                    try:
                        send_email_optional(
                            to_email=r.email,
                            subject=f"Nouveau message: {subject}",
                            body=f"Un nouveau signalement a été créé par {user.username}.\n\n{details}\n\nAccéder à la conversation: {request.host_url.rstrip('/')}/messages"
                        )
                    except Exception:
                        pass

        html = (
            "<div id='modal-root' class='modal-overlay' style='display:flex'>"
            "<div class='modal-content' style='padding:1.5rem'>"
            "<h3>Merci pour votre signalement</h3>"
            "<p>Votre message a été envoyé aux destinataires sélectionnés.</p>"
            "<div style='display:flex; gap:.5rem; justify-content:flex-end'>"
            "<a class='btn btn-primary' href='/messages'>Ouvrir la messagerie</a>"
            "<button class='btn btn-secondary' onclick=\"document.getElementById('modal-root').style.display='none'\">Fermer</button>"
            "</div>"
            "</div>"
            "</div>"
        )
        return html
    except Exception as e:
        db.session.rollback()
        return f"<div id='modal-root' class='modal-overlay' style='display:flex'><div class='modal-content'><div class='modal-header'><h3>Signaler un problème</h3></div><div class='alert alert-danger'>Erreur lors de l'envoi: {str(e)}</div></div></div>", 200


# ================== Messagerie ==================

@app.route('/messages')
def messages_home():
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return redirect(url_for('play_quiz'))
    return render_template('messages.html')


@app.route('/api/messages/list')
def api_messages_list():
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "<div class='alert alert-warning'>Connectez-vous pour voir vos messages.</div>", 200

    parts = ConversationParticipant.query.filter_by(user_id=user.id).all()
    # Récupérer conversations et derniers messages
    items = []
    for p in parts:
        conv = Conversation.query.get(p.conversation_id)
        if not conv:
            continue
        last_msg = ConversationMessage.query.filter_by(conversation_id=conv.id).order_by(ConversationMessage.created_at.desc()).first()

        # Calcul des messages non lus (même logique que le widget)
        if p.last_read_at is None:
            # Pour les nouveaux participants, compter tous les messages sauf ceux de l'utilisateur
            unread_count = ConversationMessage.query.filter(
                ConversationMessage.conversation_id == p.conversation_id,
                or_(ConversationMessage.sender_id.is_(None), ConversationMessage.sender_id != user.id)
            ).count()
        else:
            # Pour les participants existants, compter les messages après last_read_at
            unread_count = ConversationMessage.query.filter(
                ConversationMessage.conversation_id == p.conversation_id,
                ConversationMessage.created_at > p.last_read_at,
                or_(ConversationMessage.sender_id.is_(None), ConversationMessage.sender_id != user.id)
            ).count()

        items.append((conv, last_msg, unread_count))

    # Trier par date du dernier message descendant (plus récent en premier)
    items.sort(key=lambda x: x[1].created_at if x[1] else datetime.min, reverse=True)

    return render_template('partials/messages_list.html', items=items)


@app.route('/api/messages/thread/<int:conv_id>')
def api_messages_thread(conv_id: int):
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "<div class='alert alert-warning'>Connectez-vous pour voir cette conversation.</div>", 200

    part = ConversationParticipant.query.filter_by(conversation_id=conv_id, user_id=user.id).first()
    if not part:
        return "<div class='alert alert-danger'>Accès refusé.</div>", 200

    conv = Conversation.query.get(conv_id)
    if not conv:
        return "<div class='alert alert-danger'>Conversation introuvable.</div>", 200

    print(f"[THREAD] Loading thread {conv_id} for user {user.username}, last_read_at was: {part.last_read_at}")

    # Marquer comme lu
    try:
        old_last_read = part.last_read_at
        part.last_read_at = datetime.utcnow()
        db.session.commit()
        print(f"[THREAD] Updated last_read_at from {old_last_read} to {part.last_read_at}")
    except Exception as e:
        db.session.rollback()
        print(f"[THREAD] Error updating last_read_at: {e}")

    messages = ConversationMessage.query.filter_by(conversation_id=conv.id).order_by(ConversationMessage.created_at.asc()).all()
    return render_template('partials/conversation_thread.html', conversation=conv, messages=messages, me=user)


@app.route('/api/messages/mark-unread/<int:conv_id>', methods=['POST'])
def api_messages_mark_unread(conv_id: int):
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "Unauthorized", 403

    part = ConversationParticipant.query.filter_by(conversation_id=conv_id, user_id=user.id).first()
    if not part:
        return "Access denied", 403

    try:
        print(f"[MARK_UNREAD] User {user.username} marking conversation {conv_id} as unread")
        part.last_read_at = None  # Remettre à None pour marquer comme non lu
        db.session.commit()
        print(f"[MARK_UNREAD] Successfully marked conversation {conv_id} as unread for user {user.username}")
        return "", 200  # HTMX ne fait rien avec le contenu, juste le statut
    except Exception as e:
        db.session.rollback()
        print(f"[MARK_UNREAD] Error marking as unread: {e}")
        return "Error", 500


@app.route('/api/messages/delete/<int:conv_id>', methods=['POST'])
def api_messages_delete(conv_id: int):
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return redirect(url_for('play_quiz'))

    part = ConversationParticipant.query.filter_by(conversation_id=conv_id, user_id=user.id).first()
    if not part:
        # Retourner directement le HTML de la page avec un message d'erreur
        flash("Accès refusé à cette conversation.", "danger")
        return render_template('messages_content.html')

    try:
        print(f"[DELETE_CONV] User {user.username} deleting conversation {conv_id}")

        # Supprimer la participation de l'utilisateur
        db.session.delete(part)

        # Vérifier s'il reste des participants
        remaining_parts = ConversationParticipant.query.filter_by(conversation_id=conv_id).count()

        if remaining_parts == 0:
            # Plus de participants, supprimer complètement la conversation et ses messages
            print(f"[DELETE_CONV] No more participants, deleting conversation {conv_id} completely")

            # Supprimer les messages
            ConversationMessage.query.filter_by(conversation_id=conv_id).delete()

            # Supprimer les rapports/questions liés si c'est un signalement
            conv = Conversation.query.get(conv_id)
            if conv and conv.context_type == 'question_report' and conv.context_id:
                QuestionReport.query.filter_by(id=conv.context_id).delete()
            elif conv and conv.context_type == 'contact_message' and conv.context_id:
                ContactMessage.query.filter_by(id=conv.context_id).delete()

            # Supprimer la conversation
            db.session.delete(conv)
        else:
            print(f"[DELETE_CONV] {remaining_parts} participants remaining, keeping conversation {conv_id}")

        db.session.commit()
        print(f"[DELETE_CONV] Successfully deleted conversation {conv_id} for user {user.username}")

        # Retourner directement le HTML de la page messages rechargée avec un message de succès
        flash("Conversation supprimée de votre boîte de réception.", "success")
        return render_template('messages_content.html')

    except Exception as e:
        db.session.rollback()
        print(f"[DELETE_CONV] Error deleting conversation: {e}")
        flash("Erreur lors de la suppression de la conversation.", "danger")
        return render_template('messages_content.html')


@app.route('/api/messages/send', methods=['POST'])
def api_messages_send():
    user = getattr(g, 'current_user', None)
    if not user or not user.password_hash:
        return "<div class='alert alert-warning'>Connectez-vous pour envoyer un message.</div>", 200

    conv_id_raw = (request.form.get('conversation_id') or '').strip()
    content = (request.form.get('content') or '').strip()
    if not conv_id_raw.isdigit() or not content:
        return "<div class='alert alert-danger'>Données invalides.</div>", 200

    conv_id = int(conv_id_raw)
    part = ConversationParticipant.query.filter_by(conversation_id=conv_id, user_id=user.id).first()
    if not part:
        return "<div class='alert alert-danger'>Accès refusé.</div>", 200

    try:
        msg = ConversationMessage(conversation_id=conv_id, sender_id=user.id, content=content)
        db.session.add(msg)
        db.session.commit()

        # Notifier les autres participants
        other_parts = ConversationParticipant.query.filter(ConversationParticipant.conversation_id == conv_id, ConversationParticipant.user_id != user.id).all()
        if other_parts:
            recipients = User.query.filter(User.id.in_([p.user_id for p in other_parts])).all()
            conv = Conversation.query.get(conv_id)
            for r in recipients:
                prefs = r.get_preferences()
                if prefs.get('notify_email_on_message') and r.email:
                    try:
                        send_email_optional(
                            to_email=r.email,
                            subject=f"Nouveau message: {conv.subject or 'Conversation'}",
                            body=f"{user.username} a envoyé un nouveau message.\n\n{content}\n\nAccéder à la conversation: {request.host_url.rstrip('/')}/messages"
                        )
                    except Exception:
                        pass

        # Réafficher le fil
        messages = ConversationMessage.query.filter_by(conversation_id=conv_id).order_by(ConversationMessage.created_at.asc()).all()
        conv = Conversation.query.get(conv_id)
        return render_template('partials/conversation_thread.html', conversation=conv, messages=messages, me=user)
    except Exception as e:
        db.session.rollback()
        return f"<div class='alert alert-danger'>Erreur lors de l'envoi: {str(e)}</div>", 200


@app.route('/admin')
def admin_page():
    """Page d'administration avec la liste des questions"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('index.html')


@app.route('/questions')
def list_questions():
    """Retourner la liste des questions en HTML (pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    view = request.args.get('view', 'cards')
    sort_by = request.args.get('sort_by', 'updated_at')
    sort_order = request.args.get('sort_order', 'desc')

    base_query = Question.query.join(User, Question.author_id == User.id).join(BroadTheme, Question.broad_theme_id == BroadTheme.id, isouter=True).join(SpecificTheme, Question.specific_theme_id == SpecificTheme.id, isouter=True)

    questions = _apply_sorting(base_query, sort_by, sort_order).all()
    return render_template('questions_list.html', questions=questions, view=view, sort_by=sort_by, sort_order=sort_order)


# ===== Page d'analyse (Heatmap) =====

@app.route('/analysis')
def analysis_page():
    """Page d'analyse avec heatmap difficultés x thèmes."""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    # Valeurs par défaut de l'affichage
    default_mode = request.args.get('mode', 'broad')  # 'broad' ou 'specific'
    return render_template('analysis.html', default_mode=default_mode)


@app.route('/api/heatmap')
def heatmap_data():
    """Retourne un tableau heatmap HTML (HTMX) des comptes par difficulté x (thème/sous-thème)."""
    denied = _ensure_perm_api()
    if denied:
        return denied

    mode = request.args.get('mode', 'broad')  # 'broad' (thèmes) ou 'specific' (sous-thèmes)
    only_published = request.args.get('only_published') in ('1', 'true', 'yes', 'on')

    # Déterminer la liste des difficultés présentes (1..5 par défaut si vide)
    diff_query = db.session.query(Question.difficulty_level).distinct()
    if only_published:
        diff_query = diff_query.filter(Question.is_published.is_(True))
    difficulties = sorted({row[0] for row in diff_query.all() if row[0] is not None}) or [1, 2, 3, 4, 5]

    # Colonnes et agrégations
    if mode == 'specific':
        # Sous-thèmes
        join_model = SpecificTheme
        join_on = Question.specific_theme_id == SpecificTheme.id
        name_col = SpecificTheme.name
        id_col = SpecificTheme.id
        base = db.session.query(
            Question.difficulty_level.label('difficulty'),
            id_col.label('theme_id'),
            name_col.label('theme_name'),
            func.count(Question.id).label('count')
        ).outerjoin(join_model, join_on)
        if only_published:
            base = base.filter(Question.is_published.is_(True))
        rows = base.group_by('difficulty', 'theme_id', 'theme_name').all()

        # Récupérer l'ordre des sous-thèmes par nom
        themes = db.session.query(id_col, name_col).order_by(name_col.asc()).all()
    else:
        # Thèmes larges
        join_model = BroadTheme
        join_on = Question.broad_theme_id == BroadTheme.id
        name_col = BroadTheme.name
        id_col = BroadTheme.id
        base = db.session.query(
            Question.difficulty_level.label('difficulty'),
            id_col.label('theme_id'),
            name_col.label('theme_name'),
            func.count(Question.id).label('count')
        ).outerjoin(join_model, join_on)
        if only_published:
            base = base.filter(Question.is_published.is_(True))
        rows = base.group_by('difficulty', 'theme_id', 'theme_name').all()

        # Récupérer l'ordre des thèmes par nom
        themes = db.session.query(id_col, name_col).order_by(name_col.asc()).all()

    # Construire le mapping (difficulty -> theme_id -> count)
    counts = {}
    max_count = 0
    for r in rows:
        d = int(r.difficulty) if r.difficulty is not None else None
        t_id = r.theme_id
        c = int(r.count)
        if d is None or t_id is None:
            # Ignorer les entrées sans difficulté ou sans thème pour la heatmap
            continue
        counts.setdefault(d, {})[t_id] = c
        if c > max_count:
            max_count = c

    # Liste ordonnée des colonnes (thèmes) et des lignes (difficultés)
    theme_columns = [{'id': tid, 'name': tname} for tid, tname in themes]
    diff_rows = difficulties

    return render_template(
        'heatmap_table.html',
        mode=mode,
        only_published=only_published,
        theme_columns=theme_columns,
        diff_rows=diff_rows,
        counts=counts,
        max_count=max_count
    )


# ===== Export des questions =====

@app.route('/export')
def export_page():
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    themes = BroadTheme.query.order_by(BroadTheme.name.asc()).all()
    specific_themes = SpecificTheme.query.order_by(SpecificTheme.name.asc()).all()
    return render_template('export.html', themes=themes, specific_themes=specific_themes)


def _export_base_query():
    query = Question.query.join(User, Question.author_id == User.id).join(BroadTheme, Question.broad_theme_id == BroadTheme.id, isouter=True).join(SpecificTheme, Question.specific_theme_id == SpecificTheme.id, isouter=True)
    return query


def _apply_export_filters(query):
    # Filtres
    q = (request.args.get('q') or '').strip()
    if q:
        query = query.filter(
            db.or_(
                Question.question_text.contains(q),
                Question.detailed_answer.contains(q),
                Question.hint.contains(q),
                User.username.contains(q),
                BroadTheme.name.contains(q),
                SpecificTheme.name.contains(q)
            )
        )

    # Filtre par auteur
    author_filter = request.args.get('author_filter', 'mine')  # Par défaut: mes questions uniquement
    if author_filter == 'mine':
        user = getattr(g, 'current_user', None)
        if user:
            query = query.filter(Question.author_id == user.id)
        else:
            # Si pas d'utilisateur connecté, ne montrer aucune question
            query = query.filter(False)

    publication_status = request.args.get('publication_status')
    if publication_status == 'published':
        query = query.filter(Question.is_published.is_(True))
    elif publication_status == 'unpublished':
        query = query.filter(Question.is_published.is_(False))

    try:
        diff_min = int(request.args.get('diff_min')) if request.args.get('diff_min') else None
        diff_max = int(request.args.get('diff_max')) if request.args.get('diff_max') else None
    except ValueError:
        diff_min = diff_max = None
    if diff_min is not None:
        query = query.filter(Question.difficulty_level >= diff_min)
    if diff_max is not None:
        query = query.filter(Question.difficulty_level <= diff_max)

    theme_id = request.args.get('broad_theme_id')
    if theme_id and theme_id.isdigit():
        query = query.filter(Question.broad_theme_id == int(theme_id))

    specific_theme_id = request.args.get('specific_theme_id')
    if specific_theme_id and specific_theme_id.isdigit():
        query = query.filter(Question.specific_theme_id == int(specific_theme_id))

    # Filtres par date
    created_after = request.args.get('created_after')
    if created_after:
        try:
            created_after_date = datetime.strptime(created_after, '%Y-%m-%d')
            query = query.filter(Question.created_at >= created_after_date)
        except ValueError:
            pass  # Ignorer si format invalide

    created_before = request.args.get('created_before')
    if created_before:
        try:
            created_before_date = datetime.strptime(created_before, '%Y-%m-%d')
            # Inclure toute la journée (jusqu'à 23:59:59)
            created_before_date = created_before_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Question.created_at <= created_before_date)
        except ValueError:
            pass  # Ignorer si format invalide

    return query


def _serialize_question_for_export(q: Question):
    answers = (q.possible_answers or '').split('|||') if q.possible_answers else []
    return {
        'id': q.id,
        'auteur': q.author_user.username if q.author_user else None,
        'theme': q.theme.name if q.theme else None,
        'soustheme': q.specific_theme_obj.name if q.specific_theme_obj else None,
        'difficulte': q.difficulty_level,
        'question': q.question_text,
        'propositions': answers,
        'indice': q.hint,
        'reponse_detaillee': q.detailed_answer,
        'bonne_reponse_index': q.correct_answer,
        'publie': q.is_published,
        'cree_le': q.created_at.isoformat() if q.created_at else None,
        'modifie_le': q.updated_at.isoformat() if q.updated_at else None,
        'source': q.source,
    }


@app.route('/api/export/download')
def export_download():
    denied = _ensure_perm_api()
    if denied:
        return denied

    fmt = (request.args.get('format') or 'csv').lower()  # csv, json, jsonl, md
    page = int(request.args.get('page', 1) or 1)
    page_size = int(request.args.get('page_size', 200) or 200)
    page = max(page, 1)
    page_size = max(1, min(page_size, 2000))

    query = _apply_export_filters(_export_base_query()).order_by(Question.id.asc())

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    rows = [_serialize_question_for_export(q) for q in items]

    filename = f"export_questions_p{page}_n{len(rows)}.{fmt}"

    if fmt == 'json':
        data = json.dumps({'total': total, 'page': page, 'page_size': page_size, 'count': len(rows), 'items': rows}, ensure_ascii=False, indent=2)
        resp = make_response(data)
        resp.headers['Content-Type'] = 'application/json; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp

    if fmt == 'jsonl':
        lines = '\n'.join([json.dumps(r, ensure_ascii=False) for r in rows])
        resp = make_response(lines)
        resp.headers['Content-Type'] = 'application/x-ndjson; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp

    if fmt == 'md' or fmt == 'markdown':
        def md_escape(text):
            if text is None:
                return ''
            return str(text).replace('\r', '').strip()
        md_lines = []
        for r in rows:
            md_lines.append(f"### Q{r['id']} · D{r['difficulte']} · {r['theme'] or '-'} / {r['soustheme'] or '-'}")
            md_lines.append('')
            md_lines.append(md_escape(r['question']))
            md_lines.append('')
            for i, ans in enumerate(r['propositions'], start=1):
                marker = '✅' if str(i) == str(r['bonne_reponse_index'] or '') else '▫️'
                md_lines.append(f"- {marker} {md_escape(ans)}")
            if r['indice']:
                md_lines.append('')
                md_lines.append(f"Hint: {md_escape(r['indice'])}")
            if r['reponse_detaillee']:
                md_lines.append('')
                md_lines.append(f"Réponse: {md_escape(r['reponse_detaillee'])}")
            md_lines.append('')
            md_lines.append('---')
            md_lines.append('')
        data = '\n'.join(md_lines)
        resp = make_response(data)
        resp.headers['Content-Type'] = 'text/markdown; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp

    # Par défaut: CSV
    import csv
    from io import StringIO
    csv_buf = StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(['id', 'auteur', 'theme', 'soustheme', 'difficulte', 'question', 'proposition_1', 'proposition_2', 'proposition_3', 'proposition_4', 'proposition_5', 'proposition_6', 'indice', 'reponse_detaillee', 'bonne_reponse_index', 'publie'])
    for r in rows:
        props = list(r['propositions']) if r['propositions'] else []
        props = props + [''] * (6 - len(props))  # normaliser sur 6 colonnes max
        writer.writerow([
            r['id'], r['auteur'] or '', r['theme'] or '', r['soustheme'] or '', r['difficulte'] or '',
            (r['question'] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[0] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[1] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[2] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[3] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[4] or '').replace('\r', '').replace('\n', ' ').strip(),
            (props[5] or '').replace('\r', '').replace('\n', ' ').strip(),
            (r['indice'] or '').replace('\r', '').replace('\n', ' ').strip(),
            (r['reponse_detaillee'] or '').replace('\r', '').replace('\n', ' ').strip(),
            r['bonne_reponse_index'] or '',
            '1' if r['publie'] else '0'
        ])
    resp = make_response(csv_buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp

@app.route('/question/<int:question_id>/stats')
def question_stats_page(question_id: int):
    """Page admin des statistiques d'une question."""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    q = Question.query.get_or_404(question_id)

    # Statistiques globales
    total_answers = q.times_answered or 0
    total_success = q.success_count or 0
    success_rate = (total_success / total_answers * 100.0) if total_answers > 0 else 0.0

    # Répartition des réponses
    distribution = []
    if q.possible_answers:
        answers = q.possible_answers.split('|||')
        # Précharger stats
        stats_rows = QuestionAnswerStat.query.filter_by(question_id=q.id).all()
        idx_to_count = {row.answer_index: (row.selected_count or 0) for row in stats_rows}
        for i, text in enumerate(answers, start=1):
            count = int(idx_to_count.get(i, 0))
            pct = (count / total_answers * 100.0) if total_answers > 0 else 0.0
            distribution.append({
                'index': i,
                'text': text,
                'count': count,
                'percent': pct,
                'is_correct': (str(i) == str(q.correct_answer or ''))
            })

    return render_template('question_stats.html', question=q,
                           total_answers=total_answers,
                           total_success=total_success,
                           success_rate=success_rate,
                           distribution=distribution)


@app.route('/question/new')
def new_question():
    """Formulaire pour créer une nouvelle question"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_create_question'):
        return _deny_access("Permission 'can_create_question' requise")
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    specific_themes = SpecificTheme.query.join(BroadTheme).order_by(BroadTheme.name, SpecificTheme.name).all()
    countries = Country.query.order_by(Country.name).all()
    images = ImageAsset.query.order_by(ImageAsset.created_at.desc()).all()
    return render_template('question_form.html', question=None, themes=themes, specific_themes=specific_themes, countries=countries, images=images)


@app.route('/question/<int:question_id>')
def view_question(question_id):
    """Voir les détails d'une question"""
    question = Question.query.get_or_404(question_id)
    return render_template('question_detail.html', question=question)


@app.route('/question/<int:question_id>/edit')
def edit_question(question_id):
    """Formulaire pour éditer une question"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    question = Question.query.get_or_404(question_id)
    can_any = _has_perm('can_update_delete_any_question')
    can_own = _has_perm('can_update_delete_own_question')
    if not (can_any or (can_own and getattr(g, 'current_user', None) and question.author_id == g.current_user.id)):
        user = getattr(g, 'current_user', None)
        return render_template('access_denied.html', reason="Permission 'can_update_delete_own_question' ou 'can_update_delete_any_question' requise", current_user=user), 200
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    specific_themes = SpecificTheme.query.join(BroadTheme).order_by(BroadTheme.name, SpecificTheme.name).all()
    countries = Country.query.order_by(Country.name).all()
    images = ImageAsset.query.order_by(ImageAsset.created_at.desc()).all()
    return render_template('question_form.html', question=question, themes=themes, specific_themes=specific_themes, countries=countries, images=images)


@app.route('/api/question', methods=['POST'])
def create_question():
    """Créer une nouvelle question"""
    try:
        denied = _ensure_perm_api('can_create_question')
        if denied:
            return denied
        data = request.form
        
        # Traiter les réponses possibles (en conservant l'index des réponses retenues)
        possible_answers = []
        answer_images_per_answer = []  # aligné sur possible_answers ('' si pas d'image)
        links_to_add = []  # liste de tuples (answer_index, image_id)
        i = 1
        current_index = 0
        while f'answer_{i}' in data:
            answer = (data.get(f'answer_{i}', '') or '').strip()
            answer_image_token = (data.get(f'answer_image_id_{i}', '') or '').strip()
            if answer or answer_image_token:
                current_index += 1
                possible_answers.append(answer)
                if answer_image_token.isdigit():
                    image_id_int = int(answer_image_token)
                    answer_images_per_answer.append(str(image_id_int))
                    links_to_add.append((current_index, image_id_int))
                else:
                    answer_images_per_answer.append('')
            i += 1
        
        # Attribuer l'auteur en fonction des droits
        if _has_perm('can_update_delete_any_question') and (data.get('author_id') or '').isdigit():
            author_id = int(data.get('author_id'))
        else:
            author_id = g.current_user.id if getattr(g, 'current_user', None) else None

        question = Question(
            author_id=author_id,
            question_text=data.get('question_text'),
            possible_answers='|||'.join(possible_answers),
            answer_images='|||'.join(answer_images_per_answer),
            correct_answer=data.get('correct_answer'),
            detailed_answer=data.get('detailed_answer'),
            hint=data.get('hint'),
            source=data.get('source').strip() if data.get('source') else None,
            detailed_answer_image_id=int(data.get('detailed_answer_image_id')) if data.get('detailed_answer_image_id') else None,
            broad_theme_id=int(data.get('broad_theme_id')) if data.get('broad_theme_id') else None,
            specific_theme_id=int(data.get('specific_theme_id')) if data.get('specific_theme_id') else None,
            difficulty_level=int(data.get('difficulty_level', 1)),
            translation_id=int(data.get('translation_id')) if data.get('translation_id') else None,
            is_published=data.get('is_published') == 'on',
            is_private=data.get('is_private') == 'on'
        )
        
        # Gérer les pays (relation many-to-many)
        country_ids = request.form.getlist('countries')
        if country_ids:
            countries = Country.query.filter(Country.id.in_(country_ids)).all()
            question.countries = countries

        # Gérer l'image de la question (relation many-to-many, une seule image)
        question_image_id = request.form.get('question_image_id')
        if question_image_id:
            try:
                img = ImageAsset.query.get(int(question_image_id))
                if img:
                    question.images = [img]
            except ValueError:
                pass
        else:
            question.images = []
        
        # Gérer les mots-clés (relation many-to-many)
        keyword_ids = request.form.getlist('keywords')
        if keyword_ids:
            keywords = Keyword.query.filter(Keyword.id.in_([int(kid) for kid in keyword_ids if kid])).all()
            question.keywords = keywords
        
        db.session.add(question)
        db.session.flush()

        # Gérer les liens image->réponse (AnswerImageLink) avec index correct
        for answer_index, image_id in links_to_add:
            db.session.add(AnswerImageLink(question_id=question.id, answer_index=answer_index, image_id=image_id))

        db.session.commit()
        
        # Retourner la liste mise à jour
        questions = Question.query.order_by(Question.updated_at.desc()).all()
        return render_template('questions_list.html', questions=questions)
    
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/question/<int:question_id>', methods=['GET'])
def get_question_detail(question_id):
    """Récupérer le détail complet d'une question"""
    try:
        question = Question.query.get(question_id)
        if not question:
            return {'error': 'Question non trouvée'}, 404
        
        return question.to_dict()
    
    except Exception as e:
        print(f"Erreur lors de la récupération de la question {question_id}: {e}")
        return {'error': str(e)}, 500


@app.route('/api/question/<int:question_id>', methods=['PUT', 'POST'])
def update_question(question_id):
    """Mettre à jour une question existante"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        question = Question.query.get_or_404(question_id)
        can_any = _has_perm('can_update_delete_any_question')
        can_own = _has_perm('can_update_delete_own_question')
        if not (can_any or (can_own and getattr(g, 'current_user', None) and question.author_id == g.current_user.id)):
            return _deny_access("Permission 'can_update_delete_own_question' ou 'can_update_delete_any_question' requise")
        data = request.form
        
        # Traiter les réponses possibles (en conservant l'index des réponses retenues)
        possible_answers = []
        answer_images_per_answer = []  # aligné sur possible_answers ('' si pas d'image)
        links_to_add = []  # liste de tuples (answer_index, image_id)
        i = 1
        current_index = 0
        while f'answer_{i}' in data:
            answer = (data.get(f'answer_{i}', '') or '').strip()
            answer_image_token = (data.get(f'answer_image_id_{i}', '') or '').strip()
            if answer or answer_image_token:
                current_index += 1
                possible_answers.append(answer)
                if answer_image_token.isdigit():
                    image_id_int = int(answer_image_token)
                    answer_images_per_answer.append(str(image_id_int))
                    links_to_add.append((current_index, image_id_int))
                else:
                    answer_images_per_answer.append('')
            i += 1
        
        # Mettre à jour les champs
        # Changer l'auteur uniquement avec le droit global
        if can_any and (data.get('author_id') or '').isdigit():
            question.author_id = int(data.get('author_id'))
        question.question_text = data.get('question_text')
        question.possible_answers = '|||'.join(possible_answers)
        question.answer_images = '|||'.join(answer_images_per_answer)
        question.correct_answer = data.get('correct_answer')
        question.detailed_answer = data.get('detailed_answer')
        question.hint = data.get('hint')
        question.source = data.get('source').strip() if data.get('source') else None
        question.detailed_answer_image_id = int(data.get('detailed_answer_image_id')) if data.get('detailed_answer_image_id') else None
        question.broad_theme_id = int(data.get('broad_theme_id')) if data.get('broad_theme_id') else None
        question.specific_theme_id = int(data.get('specific_theme_id')) if data.get('specific_theme_id') else None
        question.difficulty_level = int(data.get('difficulty_level', 1))
        question.translation_id = int(data.get('translation_id')) if data.get('translation_id') else None
        question.is_published = data.get('is_published') == 'on'
        question.is_private = data.get('is_private') == 'on'
        question.updated_at = datetime.utcnow()
        
        # Gérer les pays (relation many-to-many)
        country_ids = request.form.getlist('countries')
        if country_ids:
            countries = Country.query.filter(Country.id.in_(country_ids)).all()
            question.countries = countries
        else:
            question.countries = []

        # Gérer l'image de la question (relation many-to-many, une seule image)
        question_image_id = request.form.get('question_image_id')
        if question_image_id:
            try:
                img = ImageAsset.query.get(int(question_image_id))
                if img:
                    question.images = [img]
            except ValueError:
                pass
        else:
            question.images = []
        
        # Gérer les mots-clés (relation many-to-many)
        keyword_ids = request.form.getlist('keywords')
        if keyword_ids:
            keywords = Keyword.query.filter(Keyword.id.in_([int(kid) for kid in keyword_ids if kid])).all()
            question.keywords = keywords
        else:
            question.keywords = []
        
        # Réinitialiser les liens image->réponse
        AnswerImageLink.query.filter_by(question_id=question.id).delete()
        db.session.flush()
        for answer_index, image_id in links_to_add:
            db.session.add(AnswerImageLink(question_id=question.id, answer_index=answer_index, image_id=image_id))

        db.session.commit()
        
        # Retourner la liste mise à jour
        questions = Question.query.order_by(Question.updated_at.desc()).all()
        return render_template('questions_list.html', questions=questions)
    
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/question/<int:question_id>', methods=['DELETE'])
def delete_question(question_id):
    """Supprimer une question"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        question = Question.query.get_or_404(question_id)
        can_any = _has_perm('can_update_delete_any_question')
        can_own = _has_perm('can_update_delete_own_question')
        if not (can_any or (can_own and getattr(g, 'current_user', None) and question.author_id == g.current_user.id)):
            return _deny_access("Permission 'can_update_delete_own_question' ou 'can_update_delete_any_question' requise")
        db.session.delete(question)
        db.session.commit()

        # Retourner la liste mise à jour
        questions = Question.query.order_by(Question.updated_at.desc()).all()
        return render_template('questions_list.html', questions=questions)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/question/<int:question_id>/toggle-status', methods=['POST'])
def toggle_question_status(question_id):
    """Changer le statut de publication d'une question"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        question = Question.query.get_or_404(question_id)
        can_any = _has_perm('can_update_delete_any_question')
        can_own = _has_perm('can_update_delete_own_question')
        if not (can_any or (can_own and getattr(g, 'current_user', None) and question.author_id == g.current_user.id)):
            return _deny_access("Permission 'can_update_delete_own_question' ou 'can_update_delete_any_question' requise")
        question.is_published = not question.is_published
        question.updated_at = datetime.utcnow()
        db.session.commit()

        # Retourner uniquement le contenu de la cellule statut mis à jour
        return render_template('question_status_cell.html', question=question)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


def _apply_sorting(query, sort_by, sort_order):
    """Appliquer le tri à la requête selon les paramètres donnés"""
    if sort_by == 'question_text':
        if sort_order == 'asc':
            return query.order_by(Question.question_text.asc())
        else:
            return query.order_by(Question.question_text.desc())
    elif sort_by == 'broad_theme':
        if sort_order == 'asc':
            return query.order_by(BroadTheme.name.asc().nulls_last())
        else:
            return query.order_by(BroadTheme.name.desc().nulls_last())
    elif sort_by == 'specific_theme':
        if sort_order == 'asc':
            return query.order_by(SpecificTheme.name.asc().nulls_last())
        else:
            return query.order_by(SpecificTheme.name.desc().nulls_last())
    elif sort_by == 'difficulty_level':
        if sort_order == 'asc':
            return query.order_by(Question.difficulty_level.asc())
        else:
            return query.order_by(Question.difficulty_level.desc())
    elif sort_by == 'is_published':
        if sort_order == 'asc':
            return query.order_by(Question.is_published.asc())
        else:
            return query.order_by(Question.is_published.desc())
    elif sort_by == 'created_at':
        if sort_order == 'asc':
            return query.order_by(Question.created_at.asc())
        else:
            return query.order_by(Question.created_at.desc())
    elif sort_by == 'author':
        if sort_order == 'asc':
            return query.order_by(User.username.asc().nulls_last())
        else:
            return query.order_by(User.username.desc().nulls_last())
    else:
        # Tri par défaut
        return query.order_by(Question.updated_at.desc())


@app.route('/api/questions/search')
def search_questions():
    """Rechercher des questions"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    query_param = request.args.get('q', '').strip()
    view = request.args.get('view', 'cards')
    sort_by = request.args.get('sort_by', 'updated_at')
    sort_order = request.args.get('sort_order', 'desc')

    base_query = Question.query.join(User, Question.author_id == User.id).join(BroadTheme, Question.broad_theme_id == BroadTheme.id, isouter=True).join(SpecificTheme, Question.specific_theme_id == SpecificTheme.id, isouter=True)

    if query_param:
        base_query = base_query.filter(
            db.or_(
                Question.question_text.contains(query_param),
                User.username.contains(query_param),
                User.username.contains(query_param),
                BroadTheme.name.contains(query_param),
                SpecificTheme.name.contains(query_param)
            )
        )

    questions = _apply_sorting(base_query, sort_by, sort_order).all()

    return render_template('questions_list.html', questions=questions, view=view, sort_by=sort_by, sort_order=sort_order)


@app.route('/api/questions/sort')
def sort_questions():
    """Trier les questions"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    view = request.args.get('view', 'cards')
    sort_by = request.args.get('sort_by', 'updated_at')
    query_param = request.args.get('q', '').strip()

    # Déterminer l'ordre de tri : si on clique sur la même colonne, on inverse l'ordre
    current_sort_by = request.args.get('current_sort_by', '')
    current_sort_order = request.args.get('current_sort_order', 'desc')

    if sort_by == current_sort_by:
        # Même colonne, on inverse l'ordre
        sort_order = 'asc' if current_sort_order == 'desc' else 'desc'
    else:
        # Nouvelle colonne, on commence par ascendant
        sort_order = 'asc'

    base_query = Question.query.join(User, Question.author_id == User.id).join(BroadTheme, Question.broad_theme_id == BroadTheme.id, isouter=True).join(SpecificTheme, Question.specific_theme_id == SpecificTheme.id, isouter=True)

    if query_param:
        base_query = base_query.filter(
            db.or_(
                Question.question_text.contains(query_param),
                User.username.contains(query_param),
                User.username.contains(query_param),
                BroadTheme.name.contains(query_param),
                SpecificTheme.name.contains(query_param)
            )
        )

    questions = _apply_sorting(base_query, sort_by, sort_order).all()

    return render_template('questions_list.html', questions=questions, view=view, sort_by=sort_by, sort_order=sort_order)


# ===== Routes pour les thèmes =====

@app.route('/api/themes')
def list_themes():
    """Retourner la liste hiérarchique des thèmes et sous-thèmes en HTML (pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    return render_template('themes_unified_list.html', themes=themes)


@app.route('/api/themes/json')
def list_themes_json():
    """Retourner la liste des thèmes en JSON (pour les selects)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    return [{
        'id': theme.id,
        'name': theme.name,
        'language': theme.language,
        'icon': theme.icon
    } for theme in themes]


@app.route('/theme/new')
def new_theme():
    """Formulaire pour créer un nouveau thème"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    embedded = request.args.get('embedded') in ('1', 'true', 'yes')
    select_id = request.args.get('select_id') or ''
    return render_template('theme_form.html', theme=None, embedded=embedded, select_id=select_id)


@app.route('/theme/<int:theme_id>/edit')
def edit_theme(theme_id):
    """Formulaire pour éditer un thème"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    theme = BroadTheme.query.get_or_404(theme_id)
    return render_template('theme_form.html', theme=theme)


@app.route('/api/theme', methods=['POST'])
def create_theme():
    """Créer un nouveau thème"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        data = request.form
        
        theme = BroadTheme(
            name=data.get('name'),
            description=data.get('description'),
            language=data.get('language', 'fr'),
            icon=data.get('icon'),
            color=data.get('color'),
            translation_id=int(data.get('translation_id')) if data.get('translation_id') else None
        )
        
        db.session.add(theme)
        db.session.commit()

        # Si formulaire embarqué (modale au-dessus d'une autre modale): renvoyer JSON
        if request.form.get('embedded') in ('1', 'true', 'yes'):
            return {
                'created_theme': {
                    'id': theme.id,
                    'name': theme.name,
                    'language': theme.language,
                    'icon': theme.icon
                },
                'select_id': request.form.get('select_id')
            }

        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/theme/<int:theme_id>', methods=['PUT', 'POST'])
def update_theme(theme_id):
    """Mettre à jour un thème existant"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        theme = BroadTheme.query.get_or_404(theme_id)
        data = request.form
        
        # Mettre à jour les champs
        theme.name = data.get('name')
        theme.description = data.get('description')
        theme.language = data.get('language', 'fr')
        theme.icon = data.get('icon')
        theme.color = data.get('color')
        theme.translation_id = int(data.get('translation_id')) if data.get('translation_id') else None
        theme.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/theme/<int:theme_id>', methods=['DELETE'])
def delete_theme(theme_id):
    """Supprimer un thème"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        theme = BroadTheme.query.get_or_404(theme_id)

        # Vérifier si des questions utilisent ce thème
        question_count = theme.questions.count()
        if question_count > 0:
            return f"Impossible de supprimer ce thème : {question_count} question(s) l'utilisent encore.", 400

        db.session.delete(theme)
        db.session.commit()

        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


# ===== Routes pour les sous-thèmes =====

@app.route('/specific-themes')
def specific_themes_page():
    """Page de gestion des sous-thèmes"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('specific_themes.html')


@app.route('/api/specific-themes')
def list_specific_themes():
    """Retourner la liste des sous-thèmes en HTML (pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    return render_template('themes_unified_list.html', themes=themes)


@app.route('/specific-theme/new')
def new_specific_theme():
    """Formulaire pour créer un nouveau sous-thème"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    embedded = request.args.get('embedded') in ('1', 'true', 'yes')
    select_id = request.args.get('select_id') or ''
    broad_theme_id = request.args.get('broad_theme_id')
    broad_themes = BroadTheme.query.order_by(BroadTheme.name).all()

    # Récupérer le thème pré-sélectionné pour afficher sa couleur
    preselected_theme = None
    if broad_theme_id:
        preselected_theme = BroadTheme.query.get(int(broad_theme_id))

    return render_template('specific_theme_form.html', specific_theme=None, broad_themes=broad_themes, embedded=embedded, select_id=select_id, preselected_broad_theme_id=broad_theme_id, preselected_theme=preselected_theme)


@app.route('/specific-theme/<int:specific_theme_id>/edit')
def edit_specific_theme(specific_theme_id):
    """Formulaire pour éditer un sous-thème"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    specific_theme = SpecificTheme.query.get_or_404(specific_theme_id)
    broad_themes = BroadTheme.query.order_by(BroadTheme.name).all()
    return render_template('specific_theme_form.html', specific_theme=specific_theme, broad_themes=broad_themes)


@app.route('/api/specific-theme', methods=['POST'])
def create_specific_theme():
    """Créer un nouveau sous-thème"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        data = request.form

        specific_theme = SpecificTheme(
            name=data.get('name'),
            description=data.get('description'),
            language=data.get('language', 'fr'),
            icon=data.get('icon'),
            color=data.get('color'),
            broad_theme_id=int(data.get('broad_theme_id')),
            translation_id=int(data.get('translation_id')) if data.get('translation_id') else None
        )

        db.session.add(specific_theme)
        db.session.commit()

        # Si formulaire embarqué (modale au-dessus d'une autre modale): renvoyer JSON
        if request.form.get('embedded') in ('1', 'true', 'yes'):
            return {
                'created_specific_theme': {
                    'id': specific_theme.id,
                    'name': specific_theme.name,
                    'broad_theme_id': specific_theme.broad_theme_id,
                    'language': specific_theme.language,
                    'icon': specific_theme.icon
                },
                'select_id': request.form.get('select_id')
            }

        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/specific-theme/<int:specific_theme_id>', methods=['PUT', 'POST'])
def update_specific_theme(specific_theme_id):
    """Mettre à jour un sous-thème existant"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        specific_theme = SpecificTheme.query.get_or_404(specific_theme_id)
        data = request.form

        # Mettre à jour les champs
        specific_theme.name = data.get('name')
        specific_theme.description = data.get('description')
        specific_theme.language = data.get('language', 'fr')
        specific_theme.icon = data.get('icon')
        specific_theme.color = data.get('color')
        specific_theme.broad_theme_id = int(data.get('broad_theme_id'))
        specific_theme.translation_id = int(data.get('translation_id')) if data.get('translation_id') else None
        specific_theme.updated_at = datetime.utcnow()

        db.session.commit()

        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/specific-theme/<int:specific_theme_id>', methods=['DELETE'])
def delete_specific_theme(specific_theme_id):
    """Supprimer un sous-thème"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        specific_theme = SpecificTheme.query.get_or_404(specific_theme_id)

        # Vérifier si des questions utilisent ce sous-thème
        question_count = specific_theme.questions.count()
        if question_count > 0:
            return f"Impossible de supprimer ce sous-thème : {question_count} question(s) l'utilisent encore.", 400

        db.session.delete(specific_theme)
        db.session.commit()

        # Retourner la liste mise à jour
        themes = BroadTheme.query.order_by(BroadTheme.name).all()
        return render_template('themes_unified_list.html', themes=themes)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/specific-themes/for-theme/')
def get_specific_themes_for_broad_theme():
    """Obtenir les sous-thèmes pour un thème large (retourne HTML pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    broad_theme_id = request.args.get('broad_theme_id')
    if broad_theme_id and broad_theme_id.isdigit():
        specific_themes = SpecificTheme.query.filter_by(broad_theme_id=int(broad_theme_id)).order_by(SpecificTheme.name).all()
    else:
        specific_themes = []
    return render_template('specific_theme_options.html', specific_themes=specific_themes)


# ===== Routes pour la gestion unifiée des thèmes =====

@app.route('/themes')
def themes_unified_page():
    """Page de gestion unifiée des thèmes et sous-thèmes"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('themes_unified.html')


# ===== Routes pour les mots-clés (Keywords) =====

@app.route('/api/keywords/json')
def list_keywords_json():
    """Retourner la liste de tous les mots-clés en JSON (pour l'autocomplétion)"""
    try:
        keywords = Keyword.query.order_by(Keyword.name).all()
        return [kw.to_dict() for kw in keywords]
    except Exception as e:
        return {'error': str(e)}, 500


@app.route('/api/keyword', methods=['POST'])
def create_keyword():
    """Créer un nouveau mot-clé"""
    try:
        # Pas besoin de vérifier les permissions ici car c'est appelé depuis le formulaire de question
        # qui a déjà ses propres contrôles de permissions
        name = request.form.get('name', '').strip()
        language = request.form.get('language', 'fr').strip()
        description = request.form.get('description', '').strip()
        
        # Validation
        if not name:
            return {'error': 'Le nom du mot-clé est requis'}, 400
        
        # Vérifier si le mot-clé existe déjà (normalisation pour éviter doublons)
        # Normaliser: enlever accents, espaces, traits d'union, mettre en minuscules
        normalized_name = unidecode(name.lower()).replace('-', '').replace(' ', '').replace('_', '')
        
        existing_keywords = Keyword.query.all()
        for existing in existing_keywords:
            existing_normalized = unidecode(existing.name.lower()).replace('-', '').replace(' ', '').replace('_', '')
            if existing_normalized == normalized_name:
                return {
                    'error': 'Un mot-clé similaire existe déjà',
                    'existing_keyword': existing.to_dict()
                }, 409
        
        # Créer le nouveau mot-clé
        keyword = Keyword(
            name=name,
            language=language,
            description=description if description else None
        )
        db.session.add(keyword)
        db.session.commit()
        
        return {
            'success': True,
            'keyword': keyword.to_dict(),
            'message': f'Mot-clé "{name}" créé avec succès'
        }, 201
        
    except Exception as e:
        db.session.rollback()
        return {'error': str(e)}, 500


# ===== Routes pour les utilisateurs =====

@app.route('/users')
def users_page():
    """Page de gestion des utilisateurs"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_users'):
        return redirect(url_for('play_quiz'))
    return render_template('users.html')


@app.route('/api/users')
def list_users():
    """Retourner la liste des utilisateurs en HTML (pour HTMX)"""
    denied = _ensure_perm_api('can_manage_users')
    if denied:
        return denied
    users = User.query.filter_by(is_active=True).order_by(User.username).all()
    return render_template('users_list.html', users=users)


@app.route('/user/new')
def new_user():
    """Formulaire pour créer un nouvel utilisateur"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_users'):
        return _deny_access("Permission 'can_manage_users' requise")
    profiles = Profile.query.order_by(Profile.name).all()
    return render_template('user_form.html', user=None, profiles=profiles)


@app.route('/user/<int:user_id>/edit')
def edit_user(user_id):
    """Formulaire pour éditer un utilisateur"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_users'):
        return _deny_access("Permission 'can_manage_users' requise")
    user = User.query.get_or_404(user_id)
    profiles = Profile.query.order_by(Profile.name).all()
    return render_template('user_form.html', user=user, profiles=profiles)


@app.route('/api/user', methods=['POST'])
def create_user():
    """Créer un nouvel utilisateur"""
    try:
        denied = _ensure_perm_api('can_manage_users')
        if denied:
            return denied
        data = request.form

        # Validation du mot de passe pour les profils admin
        profile_id = data.get('profile_id')
        password = (data.get('password') or '').strip()

        if profile_id and profile_id.isdigit():
            profile = Profile.query.get(int(profile_id))
            if profile and profile.can_access_admin and not password:
                return "Mot de passe requis pour les utilisateurs avec accès administration", 400

        user = User(
            username=data.get('username'),
            email=data.get('email') or None,
            is_active=data.get('is_active') == 'on',
            profile_id=(int(profile_id) if profile_id and profile_id.isdigit() else None)
        )

        # Définir le mot de passe si fourni
        if password:
            user.password_hash = generate_password_hash(password)

        db.session.add(user)
        db.session.commit()

        # Retourner la liste mise à jour
        users = User.query.filter_by(is_active=True).order_by(User.username).all()
        return render_template('users_list.html', users=users)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/user/<int:user_id>', methods=['PUT', 'POST'])
def update_user(user_id):
    """Mettre à jour un utilisateur existant"""
    try:
        denied = _ensure_perm_api('can_manage_users')
        if denied:
            return denied
        user = User.query.get_or_404(user_id)
        data = request.form

        # Validation du mot de passe pour les profils admin
        profile_id = data.get('profile_id')
        password = (data.get('password') or '').strip()

        if profile_id and profile_id.isdigit():
            profile = Profile.query.get(int(profile_id))
            if profile and profile.can_access_admin and not password and not user.password_hash:
                return "Mot de passe requis pour les utilisateurs avec accès administration", 400

        # Validation supplémentaire : si on attribue un profil admin à un utilisateur sans mot de passe
        new_profile_id = int(profile_id) if profile_id and profile_id.isdigit() else None
        if new_profile_id and new_profile_id != user.profile_id:
            new_profile = Profile.query.get(new_profile_id)
            if new_profile and new_profile.can_access_admin and not user.password_hash and not password:
                return "Impossible d'attribuer un profil admin sans mot de passe. Définissez d'abord un mot de passe.", 400

        # Mettre à jour les champs
        user.username = data.get('username')
        user.email = data.get('email') or None
        user.is_active = data.get('is_active') == 'on'
        user.profile_id = new_profile_id

        # Mettre à jour le mot de passe si fourni
        if password:
            user.password_hash = generate_password_hash(password)

        db.session.commit()

        # Retourner la liste mise à jour
        users = User.query.filter_by(is_active=True).order_by(User.username).all()
        return render_template('users_list.html', users=users)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/user/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """Désactiver un utilisateur (soft delete)"""
    try:
        denied = _ensure_perm_api('can_manage_users')
        if denied:
            return denied
        user = User.query.get_or_404(user_id)

        # Vérifier si l'utilisateur a des questions
        question_count = user.questions.count()
        if question_count > 0:
            return f"Impossible de supprimer cet utilisateur : {question_count} question(s) lui appartiennent encore.", 400

        # Soft delete : désactiver au lieu de supprimer
        user.is_active = False
        db.session.commit()

        # Retourner la liste mise à jour
        users = User.query.filter_by(is_active=True).order_by(User.username).all()
        return render_template('users_list.html', users=users)

    except Exception as e:
        return f"Erreur: {str(e)}", 400


# ===== Routes pour les profils (permissions) =====

@app.route('/profiles')
def profiles_page():
    """Page de gestion des profils"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_profiles'):
        return redirect(url_for('play_quiz'))
    return render_template('profiles.html')


@app.route('/api/profiles')
def list_profiles():
    """Retourner la liste des profils en HTML (pour HTMX)"""
    denied = _ensure_perm_api('can_manage_profiles')
    if denied:
        return denied
    profiles = Profile.query.order_by(Profile.name).all()
    return render_template('profiles_list.html', profiles=profiles)


@app.route('/profile/new')
def new_profile():
    """Formulaire pour créer un nouveau profil"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_profiles'):
        return _deny_access("Permission 'can_manage_profiles' requise")
    return render_template('profile_form.html', profile=None)


@app.route('/profile/<int:profile_id>/edit')
def edit_profile(profile_id: int):
    """Formulaire pour éditer un profil"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_manage_profiles'):
        return _deny_access("Permission 'can_manage_profiles' requise")
    profile = Profile.query.get_or_404(profile_id)
    return render_template('profile_form.html', profile=profile)


def _bool_from_form(key: str) -> bool:
    return request.form.get(key) == 'on'


@app.route('/api/profile', methods=['POST'])
def create_profile():
    """Créer un nouveau profil"""
    try:
        denied = _ensure_perm_api('can_manage_profiles')
        if denied:
            return denied
        data = request.form
        name = (data.get('name') or '').strip()
        if not name:
            return "Nom requis", 400

        profile = Profile(
            name=name,
            description=(data.get('description') or '').strip() or None,
            can_access_admin=_bool_from_form('can_access_admin'),
            can_create_question=_bool_from_form('can_create_question'),
            can_update_delete_own_question=_bool_from_form('can_update_delete_own_question'),
            can_update_delete_any_question=_bool_from_form('can_update_delete_any_question'),
            can_create_rule=_bool_from_form('can_create_rule'),
            can_update_delete_own_rule=_bool_from_form('can_update_delete_own_rule'),
            can_update_delete_any_rule=_bool_from_form('can_update_delete_any_rule'),
            can_manage_users=_bool_from_form('can_manage_users'),
            can_manage_profiles=_bool_from_form('can_manage_profiles'),
        )

        db.session.add(profile)
        db.session.commit()

        profiles = Profile.query.order_by(Profile.name).all()
        return render_template('profiles_list.html', profiles=profiles)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/profile/<int:profile_id>', methods=['PUT', 'POST'])
def update_profile(profile_id: int):
    """Mettre à jour un profil existant"""
    try:
        denied = _ensure_perm_api('can_manage_profiles')
        if denied:
            return denied
        profile = Profile.query.get_or_404(profile_id)
        data = request.form

        name = (data.get('name') or '').strip()
        if name:
            profile.name = name
        profile.description = (data.get('description') or '').strip() or None
        profile.can_access_admin = _bool_from_form('can_access_admin')
        profile.can_create_question = _bool_from_form('can_create_question')
        profile.can_update_delete_own_question = _bool_from_form('can_update_delete_own_question')
        profile.can_update_delete_any_question = _bool_from_form('can_update_delete_any_question')
        profile.can_create_rule = _bool_from_form('can_create_rule')
        profile.can_update_delete_own_rule = _bool_from_form('can_update_delete_own_rule')
        profile.can_update_delete_any_rule = _bool_from_form('can_update_delete_any_rule')
        profile.can_manage_users = _bool_from_form('can_manage_users')
        profile.can_manage_profiles = _bool_from_form('can_manage_profiles')
        profile.updated_at = datetime.utcnow()

        db.session.commit()

        profiles = Profile.query.order_by(Profile.name).all()
        return render_template('profiles_list.html', profiles=profiles)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/profile/<int:profile_id>', methods=['DELETE'])
def delete_profile(profile_id: int):
    """Supprimer un profil"""
    try:
        denied = _ensure_perm_api('can_manage_profiles')
        if denied:
            return denied
        profile = Profile.query.get_or_404(profile_id)
        # Empêcher la suppression si des utilisateurs utilisent ce profil
        if profile.users.count() > 0:
            return "Impossible de supprimer: des utilisateurs utilisent ce profil.", 400

        db.session.delete(profile)
        db.session.commit()

        profiles = Profile.query.order_by(Profile.name).all()
        return render_template('profiles_list.html', profiles=profiles)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


# ============ Routes pour la gestion des Pays ============

@app.route('/countries')
def countries():
    """Page de gestion des pays"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('countries.html')


@app.route('/api/countries')
def list_countries_api():
    """Retourner la liste des pays en HTML (pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    search = request.args.get('search', '')
    query = Country.query
    
    if search:
        query = query.filter(Country.name.like(f'%{search}%'))
    
    countries = query.order_by(Country.name).all()
    return render_template('countries_list.html', countries=countries)


@app.route('/country/new')
def new_country():
    """Formulaire pour créer un nouveau pays"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    countries = Country.query.order_by(Country.name).all()
    return render_template('country_form.html', country=None, countries=countries)


@app.route('/country/<int:country_id>/edit')
def edit_country(country_id):
    """Formulaire pour éditer un pays"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    country = Country.query.get_or_404(country_id)
    countries = Country.query.order_by(Country.name).all()
    return render_template('country_form.html', country=country, countries=countries)


@app.route('/api/country', methods=['POST'])
def create_country():
    """Créer un nouveau pays"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        data = request.form
        
        country = Country(
            name=data.get('name'),
            code=data.get('code'),
            flag=data.get('flag'),
            language=data.get('language', 'fr'),
            description=data.get('description'),
            translation_id=int(data.get('translation_id')) if data.get('translation_id') else None
        )
        
        db.session.add(country)
        db.session.commit()
        
        # Retourner la liste mise à jour
        countries = Country.query.order_by(Country.name).all()
        return render_template('countries_list.html', countries=countries)
    
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/country/<int:country_id>', methods=['PUT', 'POST'])
def update_country(country_id):
    """Mettre à jour un pays existant"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        country = Country.query.get_or_404(country_id)
        data = request.form
        
        # Mettre à jour les champs
        country.name = data.get('name')
        country.code = data.get('code')
        country.flag = data.get('flag')
        country.language = data.get('language', 'fr')
        country.description = data.get('description')
        country.translation_id = int(data.get('translation_id')) if data.get('translation_id') else None
        country.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Retourner la liste mise à jour
        countries = Country.query.order_by(Country.name).all()
        return render_template('countries_list.html', countries=countries)
    
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/country/<int:country_id>', methods=['DELETE'])
def delete_country(country_id):
    """Supprimer un pays"""
    try:
        denied = _ensure_perm_api()
        if denied:
            return denied
        country = Country.query.get_or_404(country_id)
        
        # Vérifier si le pays est utilisé dans des questions
        question_count = country.questions.count()
        if question_count > 0:
            return f"Impossible de supprimer ce pays : {question_count} question(s) l'utilisent encore.", 400
        
        db.session.delete(country)
        db.session.commit()
        
        countries = Country.query.order_by(Country.name).all()
        return render_template('countries_list.html', countries=countries)
    
    except Exception as e:
        return f"Erreur: {str(e)}", 400


# ============ Interface de Quiz (Jouer) ============

@app.route('/quiz/<slug>')
def play_quiz_with_rules(slug: str):
    """Redirige vers la page de jeu avec un set de règles prédéfini."""
    return redirect(f'/play?rule_set={slug}')

def _apply_quiz_filters(query, params):
    """Appliquer les filtres du quiz (thèmes, pays, difficulté) au query de base."""
    rule_set_slug = (params.get('rule_set') or '').strip()
    if rule_set_slug:
        # Appliquer les règles du set
        rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()
        if rule_set:
            # Difficultés autorisées
            allowed_diffs = rule_set.get_allowed_difficulties()
            if allowed_diffs:
                query = query.filter(Question.difficulty_level.in_(allowed_diffs))

            # Thèmes larges
            if not rule_set.use_all_broad_themes and rule_set.allowed_broad_themes:
                theme_ids = [t.id for t in rule_set.allowed_broad_themes]
                query = query.filter(Question.broad_theme_id.in_(theme_ids))

            # Sous-thèmes
            if not rule_set.use_all_specific_themes and rule_set.allowed_specific_themes:
                sub_theme_ids = [st.id for st in rule_set.allowed_specific_themes]
                query = query.filter(Question.specific_theme_id.in_(sub_theme_ids))

            # Note: pas de filtre pays pour l'instant dans les sets de règles
    else:
        # Mode manuel - appliquer les filtres classiques
        broad_theme_id = (params.get('broad_theme_id') or '').strip()
        if broad_theme_id.isdigit():
            query = query.filter(Question.broad_theme_id == int(broad_theme_id))

        specific_theme_id = (params.get('specific_theme_id') or '').strip()
        if specific_theme_id.isdigit():
            query = query.filter(Question.specific_theme_id == int(specific_theme_id))

        country_id = (params.get('country_id') or '').strip()
        if country_id.isdigit():
            query = query.filter(Question.countries.any(Country.id == int(country_id)))

        difficulty_level = (params.get('difficulty_level') or '').strip()
        if difficulty_level.isdigit():
            query = query.filter(Question.difficulty_level == int(difficulty_level))

    return query


def _interleave_round_robin(lists_by_difficulty):
    """Intercale les listes de questions par difficulté (round-robin) pour varier l'ordre.
    Entrée: dict[int,list[int]]
    Sortie: list[int]
    """
    # Convertir en liste de listes en conservant un ordre stable des clés
    difficulties = sorted(lists_by_difficulty.keys())
    buckets = [list(lists_by_difficulty[d]) for d in difficulties if lists_by_difficulty.get(d)]
    result = []
    # Tant qu'il reste des éléments dans au moins un bucket
    while any(buckets):
        next_buckets = []
        for bucket in buckets:
            if bucket:
                result.append(bucket.pop(0))
            # garder le bucket s'il reste des éléments
            if bucket:
                next_buckets.append(bucket)
        buckets = next_buckets
    return result


def _quiz_session_keys(rule_set_slug: str):
    """Construit des clés de session isolées par utilisateur et par set.
    Retourne (playlist_key, index_key, score_key, correct_key, breakdown_key, streak_key, perfect_key, user_id_str)
    """
    user_id_str = str(g.current_user.id) if getattr(g, 'current_user', None) else 'anon'
    prefix = f"{user_id_str}:{rule_set_slug}"
    playlist_key = f"quiz_playlist:{prefix}"
    index_key = f"quiz_playlist_index:{prefix}"
    score_key = f"quiz_score:{prefix}"
    correct_key = f"quiz_correct_answers:{prefix}"
    breakdown_key = f"quiz_score_breakdown:{prefix}"
    streak_key = f"quiz_combo_streak:{prefix}"
    perfect_key = f"quiz_perfect_awarded:{prefix}"
    return playlist_key, index_key, score_key, correct_key, breakdown_key, streak_key, perfect_key, user_id_str


def _append_score_breakdown(breakdown_key: str, event: dict):
    """Ajoute un événement de score dans la liste stockée en session."""
    try:
        history = session.get(breakdown_key)
        if not isinstance(history, list):
            history = []
        history.append(event)
        session[breakdown_key] = history
    except Exception as exc:
        print(f"[QUIZ SCORE] Impossible d'ajouter le breakdown: {exc}")


def _get_user_answered_keywords(user_id: int) -> set[int]:
    """Récupère les IDs de tous les keywords déjà répondus par l'utilisateur."""
    if not user_id:
        return set()
    
    try:
        # Récupérer toutes les questions déjà répondues
        answered_question_ids = {row.question_id for row in 
                                 UserQuestionStat.query.with_entities(UserQuestionStat.question_id)
                                 .filter_by(user_id=user_id).all()}
        
        if not answered_question_ids:
            return set()
        
        # Récupérer les keywords de ces questions
        from sqlalchemy import select
        keyword_ids = set()
        result = db.session.execute(
            select(db.literal_column('keyword_id'))
            .select_from(db.text('question_keywords'))
            .where(db.literal_column('question_id').in_(answered_question_ids))
        )
        keyword_ids = {row[0] for row in result}
        return keyword_ids
    except Exception as e:
        print(f"[KEYWORDS] Erreur lors de la récupération des keywords répondus: {e}")
        return set()


def _select_questions_with_keyword_logic(
    candidate_ids: list[int],
    seen_question_ids: set[int],
    used_keywords: set[int],
    answered_keywords: set[int],
    prevent_duplicate_keywords: bool,
    quota: int
) -> tuple[list[int], set[int], dict[str, any]]:
    """
    Sélectionne les questions en respectant la logique des keywords.
    
    Priorités (par ordre d'importance):
    1. Condition QuizRuleSet (ABSOLU) - déjà appliqué dans candidate_ids
    2. Pas de doublons de keywords dans le quiz (si prevent_duplicate_keywords)
    3. Pas de questions déjà répondues
    4. Pas de keywords déjà répondus
    
    Retourne: (selected_ids, used_keywords_updated, stats)
    """
    if not candidate_ids or quota <= 0:
        return [], used_keywords, {'perfect': True, 'conditions_met': []}
    
    # Charger toutes les questions candidates avec leurs keywords
    candidates = Question.query.filter(Question.id.in_(candidate_ids)).options(
        db.joinedload(Question.keywords)
    ).all()
    
    # Stats pour le debug
    stats = {
        'perfect': True,
        'total_candidates': len(candidates),
        'conditions_met': [],
        'fallback_used': []
    }
    
    selected_ids = []
    current_used_keywords = set(used_keywords)
    
    # Fonction pour scorer une question selon les priorités
    def score_question(q: Question) -> tuple:
        """Retourne un tuple de score (plus élevé = meilleur). Format: (prio1, prio2, prio3, prio4)"""
        q_keywords = {kw.id for kw in q.keywords}
        
        # Priorité 1: Pas de doublons de keywords (si activé)
        if prevent_duplicate_keywords and q_keywords:
            has_duplicate_keyword = bool(q_keywords & current_used_keywords)
        else:
            has_duplicate_keyword = False
        
        # Priorité 2: Question non répondue
        is_unseen = q.id not in seen_question_ids
        
        # Priorité 3: Keywords non répondus
        if q_keywords and answered_keywords:
            has_unanswered_keywords = bool(q_keywords & answered_keywords)
        else:
            has_unanswered_keywords = False
        
        # Questions sans keywords ont un bonus (pas de risque de doublon)
        no_keywords = len(q_keywords) == 0
        
        # Retourner score (format: pas de doublon keyword, non vue, pas keyword répondu, sans keyword)
        return (
            not has_duplicate_keyword,  # Vrai = 1, Faux = 0 (on veut True en premier)
            is_unseen,
            not has_unanswered_keywords,
            no_keywords
        )
    
    # Trier les candidats par score (du meilleur au pire)
    sorted_candidates = sorted(candidates, key=score_question, reverse=True)
    
    # Sélectionner jusqu'au quota
    for q in sorted_candidates:
        if len(selected_ids) >= quota:
            break
        
        q_keywords = {kw.id for kw in q.keywords}
        
        # Vérifier si on respecte toutes les conditions
        conditions_perfect = True
        
        # Condition 2: Pas de doublons de keywords
        if prevent_duplicate_keywords and q_keywords and (q_keywords & current_used_keywords):
            conditions_perfect = False
            stats['fallback_used'].append('keyword_duplicate')
        
        # Condition 3: Question non répondue
        if q.id in seen_question_ids:
            conditions_perfect = False
            stats['fallback_used'].append('question_already_seen')
        
        # Condition 4: Keywords non répondus
        if q_keywords and answered_keywords and (q_keywords & answered_keywords):
            conditions_perfect = False
            stats['fallback_used'].append('keyword_already_answered')
        
        if not conditions_perfect:
            stats['perfect'] = False
        
        selected_ids.append(q.id)
        current_used_keywords.update(q_keywords)
    
    # Statistiques finales
    if stats['perfect']:
        stats['conditions_met'] = ['Toutes les conditions respectées ✅']
    else:
        fallback_counts = {}
        for fb in stats['fallback_used']:
            fallback_counts[fb] = fallback_counts.get(fb, 0) + 1
        stats['conditions_met'] = [
            f"⚠️ {count}x {reason.replace('_', ' ')}" 
            for reason, count in fallback_counts.items()
        ]
    
    return selected_ids, current_used_keywords, stats


def _generate_quiz_playlist(rule_set: QuizRuleSet, current_user_id: int | None) -> list[int]:
    """
    Génère la playlist (liste d'IDs de questions) pour un quiz à longueur fixe.
    
    Priorités de sélection:
    1. Respecter les conditions du QuizRuleSet (ABSOLU)
    2. Éviter les doublons de keywords dans le quiz
    3. Éviter les questions déjà répondues
    4. Éviter les keywords déjà répondus
    
    En mode 'manual': réordonne la liste sélectionnée en appliquant la logique keywords.
    En mode 'auto': respecte les quotas par difficulté avec gestion keywords.
    """
    try:
        print(f"\n[QUIZ PLAYLIST] === Génération playlist pour {rule_set.name} ===")
        
        # Récupérer les IDs déjà vus par l'utilisateur (si connecté)
        seen_ids = set()
        answered_keywords = set()
        if current_user_id:
            seen_ids = {row.question_id for row in 
                       UserQuestionStat.query.with_entities(UserQuestionStat.question_id)
                       .filter_by(user_id=current_user_id).all()}
            answered_keywords = _get_user_answered_keywords(current_user_id)
            print(f"[QUIZ PLAYLIST] Utilisateur {current_user_id}: {len(seen_ids)} questions vues, {len(answered_keywords)} keywords répondus")
        
        prevent_duplicate_keywords = rule_set.prevent_duplicate_keywords
        print(f"[QUIZ PLAYLIST] Prévention doublons keywords: {'OUI' if prevent_duplicate_keywords else 'NON'}")

        # Mode manuel: partir de la sélection explicite
        if rule_set.question_selection_mode == 'manual' and rule_set.selected_questions:
            print(f"[QUIZ PLAYLIST] Mode MANUEL: {len(rule_set.selected_questions)} questions sélectionnées")
            selected = [q for q in rule_set.selected_questions if q.is_published]
            candidate_ids = [q.id for q in selected]
            
            # Appliquer la logique keywords sur toute la sélection
            playlist, _, stats = _select_questions_with_keyword_logic(
                candidate_ids=candidate_ids,
                seen_question_ids=seen_ids,
                used_keywords=set(),
                answered_keywords=answered_keywords,
                prevent_duplicate_keywords=prevent_duplicate_keywords,
                quota=len(candidate_ids)
            )
            
            # Logs
            if stats['perfect']:
                print(f"[QUIZ PLAYLIST] ✅ CONDITIONS PARFAITES: {', '.join(stats['conditions_met'])}")
            else:
                print(f"[QUIZ PLAYLIST] ⚠️ COMPROMIS NÉCESSAIRES:")
                for condition in stats['conditions_met']:
                    print(f"[QUIZ PLAYLIST]    {condition}")
            
            print(f"[QUIZ PLAYLIST] Playlist générée: {len(playlist)} questions")
            return playlist

        # Mode auto: quotas par difficulté et filtres de thèmes
        qmap = rule_set.get_questions_per_difficulty() or {}
        allowed_diffs = rule_set.get_allowed_difficulties() or [1, 2, 3, 4, 5]
        print(f"[QUIZ PLAYLIST] Mode AUTO: difficultés {allowed_diffs}, quotas {qmap}")
        order_mode = getattr(rule_set, 'question_order_mode', 'difficulty_ascending') or 'difficulty_ascending'
        if order_mode not in ['difficulty_ascending', 'full_shuffle']:
            order_mode = 'difficulty_ascending'
        print(f"[QUIZ PLAYLIST] Ordre des questions: {order_mode}")

        # Construire la requête de base selon le set de règles
        base_params = {'rule_set': rule_set.slug}
        base_query = _apply_quiz_filters(Question.query.filter(Question.is_published.is_(True)), base_params)

        # Préparer par difficulté avec logique keywords
        per_diff_ids: dict[int, list[int]] = {}
        used_keywords_global = set()
        all_stats = []
        
        for d in allowed_diffs:
            quota = int(qmap.get(str(d), 0) or 0)
            if quota <= 0:
                per_diff_ids[d] = []
                continue

            print(f"[QUIZ PLAYLIST] Difficulté {d}: quota={quota}")
            
            q_for_diff = base_query.filter(Question.difficulty_level == d)
            candidates = q_for_diff.with_entities(Question.id).all()
            candidate_ids = [row.id for row in candidates]
            
            print(f"[QUIZ PLAYLIST]   Candidats disponibles: {len(candidate_ids)}")
            
            # Appliquer la logique keywords
            chosen, used_keywords_global, stats = _select_questions_with_keyword_logic(
                candidate_ids=candidate_ids,
                seen_question_ids=seen_ids,
                used_keywords=used_keywords_global,
                answered_keywords=answered_keywords,
                prevent_duplicate_keywords=prevent_duplicate_keywords,
                quota=quota
            )
            
            per_diff_ids[d] = chosen
            all_stats.append({
                'difficulty': d,
                'quota': quota,
                'selected': len(chosen),
                'perfect': stats['perfect'],
                'conditions': stats['conditions_met']
            })
            
            print(f"[QUIZ PLAYLIST]   Sélectionnés: {len(chosen)}/{quota}")
            if not stats['perfect']:
                for condition in stats['conditions_met']:
                    print(f"[QUIZ PLAYLIST]     {condition}")

        # Construire la playlist selon le mode d'ordre choisi
        if order_mode == 'full_shuffle':
            playlist = []
            for diff in per_diff_ids:
                playlist.extend(per_diff_ids[diff])
            random.shuffle(playlist)
        else:
            playlist = []
            for diff in sorted(per_diff_ids.keys()):
                bucket = list(per_diff_ids.get(diff) or [])
                if len(bucket) > 1:
                    random.shuffle(bucket)
                playlist.extend(bucket)

        expected_total = sum(int(qmap.get(str(d), 0) or 0) for d in allowed_diffs)
        
        # Logs finaux
        print(f"\n[QUIZ PLAYLIST] === RÉSUMÉ FINAL ===")
        print(f"[QUIZ PLAYLIST] Playlist générée: {len(playlist)}/{expected_total} questions")
        
        # Vérifier si toutes les conditions sont parfaites
        all_perfect = all(stat['perfect'] for stat in all_stats)
        if all_perfect:
            print(f"[QUIZ PLAYLIST] ✅ CONDITIONS PARFAITES pour toutes les questions !")
        else:
            print(f"[QUIZ PLAYLIST] ⚠️ COMPROMIS NÉCESSAIRES:")
            for stat in all_stats:
                if not stat['perfect']:
                    print(f"[QUIZ PLAYLIST]   Difficulté {stat['difficulty']}: {', '.join(stat['conditions'])}")
        
        if len(playlist) < expected_total:
            print(f"[QUIZ PLAYLIST] ⚠️ Playlist incomplète. Pool insuffisant pour certains quotas.")
        
        print(f"[QUIZ PLAYLIST] Keywords uniques utilisés: {len(used_keywords_global)}")
        print(f"[QUIZ PLAYLIST] ==================\n")

        return playlist
    except Exception as e:
        print(f"[QUIZ PLAYLIST] ❌ ERREUR génération playlist: {e}")
        import traceback
        traceback.print_exc()
        return []


def _get_user_double_click_preference() -> bool:
    try:
        if getattr(g, 'current_user', None):
            prefs = g.current_user.get_preferences()
            if 'double_click_validation' in prefs:
                return bool(prefs.get('double_click_validation'))
    except Exception:
        pass
    return True


@app.route('/play')
def play_quiz():
    """Page pour choisir un set de règles et jouer au quiz."""
    rule_set = None
    rule_set_slug = request.args.get('rule_set', '').strip()
    auto_start_param = (request.args.get('auto_start') or '').strip().lower()
    auto_start = auto_start_param in ('1', 'true', 'yes', 'on')
    if rule_set_slug:
        rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()
    else:
        # Si on arrive sans set explicite et qu'il existait une session en cours, l'abandonner
        if getattr(g, 'current_user', None):
            try:
                in_prog = UserQuizSession.query.filter_by(user_id=g.current_user.id, status='in_progress').all()
                for s in in_prog:
                    print(f"[QUIZ SESSION] Abandon session {s.id} while entering /play without specific rule_set (user={s.user_id})")
                    s.status = 'abandoned'
                    s.updated_at = datetime.utcnow()
                if in_prog:
                    db.session.commit()
            except Exception:
                db.session.rollback()

    # Récupérer tous les sets de règles actifs
    rule_sets = QuizRuleSet.query.filter_by(is_active=True).order_by(QuizRuleSet.name).all()

    quick_double_click_pref = _get_user_double_click_preference()
    if 'quick_double_click_enabled' in session:
        quick_double_click_enabled = bool(session.get('quick_double_click_enabled'))
    else:
        quick_double_click_enabled = quick_double_click_pref
        session['quick_double_click_enabled'] = quick_double_click_enabled

    return render_template('play.html',
                           rule_sets=rule_sets,
                           rule_set=rule_set,
                           quick_double_click=quick_double_click_enabled,
                           auto_start=auto_start)


@app.route('/api/quiz/next')
def next_quiz_question():
    """Retourne la prochaine question du quiz en consommant une playlist pré-générée.
    Si aucune playlist n'existe encore pour ce set, la génère et la stocke en session.
    """
    try:
        params = request.args
        rule_set_slug = (params.get('rule_set') or '').strip()
        history_raw = (params.get('history') or '').strip()
        quick_double_click_param = params.get('quick_double_click')
        if quick_double_click_param is not None:
            quick_double_click = quick_double_click_param.lower() == 'true'
            session['quick_double_click_enabled'] = quick_double_click
        elif 'quick_double_click_enabled' in session:
            quick_double_click = bool(session.get('quick_double_click_enabled'))
        else:
            quick_double_click = _get_user_double_click_preference()
            session['quick_double_click_enabled'] = quick_double_click
        history_ids = []
        if history_raw:
            for token in history_raw.split(','):
                token = token.strip()
                if token.isdigit():
                    history_ids.append(int(token))

        rule_set = None
        if rule_set_slug:
            rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()

        # Mode playlist: construire/charger la playlist en session (clé par utilisateur)
        playlist_session_key = playlist_index_key = score_session_key = correct_answers_session_key = breakdown_session_key = streak_session_key = perfect_session_key = user_ns = None
        if rule_set:
            (
                playlist_session_key,
                playlist_index_key,
                score_session_key,
                correct_answers_session_key,
                breakdown_session_key,
                streak_session_key,
                perfect_session_key,
                user_ns,
            ) = _quiz_session_keys(rule_set.slug)

        question = None
        total_questions = 0
        if rule_set:
            # Si pas encore de playlist, la générer
            playlist: list[int] = session.get(playlist_session_key) or []
            # Si démarrage d'une nouvelle partie (history vide) OU playlist absente, régénérer
            if (not history_raw) or (not playlist):
                playlist = _generate_quiz_playlist(rule_set, g.current_user.id if getattr(g, 'current_user', None) else None)
                session[playlist_session_key] = playlist
                session[playlist_index_key] = 0
                # Reset score/correct pour ce namespace utilisateur+set
                session[score_session_key] = 0
                session[correct_answers_session_key] = 0
                if breakdown_session_key:
                    session[breakdown_session_key] = []
                if streak_session_key:
                    session[streak_session_key] = 0
                if perfect_session_key:
                    session[perfect_session_key] = False
                print(f"[QUIZ PLAYLIST] Générée (reset={not bool(history_raw)}) pour user={user_ns} set='{rule_set.slug}' (len={len(playlist)}): {playlist}")

                # Démarrer une UserQuizSession si utilisateur connecté
                if getattr(g, 'current_user', None):
                    try:
                        # Clore toute session précédente en cours pour ce set
                        prev = (UserQuizSession.query
                                .filter_by(user_id=g.current_user.id, rule_set_id=rule_set.id, status='in_progress')
                                .all())
                        for s in prev:
                            print(f"[QUIZ SESSION] Abandon in-progress session {s.id} for rule_set {s.rule_set_id} before starting new session (user={s.user_id})")
                            s.status = 'abandoned'
                            s.updated_at = datetime.utcnow()
                        # Créer une nouvelle session
                        new_session = UserQuizSession(
                            user_id=g.current_user.id,
                            rule_set_id=rule_set.id,
                            status='in_progress',
                            total_questions=len(playlist),
                            answered_count=0,
                            correct_count=0,
                            total_score=0
                        )
                        db.session.add(new_session)
                        db.session.commit()
                        print(f"[QUIZ SESSION] Started new session {new_session.id} for rule_set {rule_set.id} (user={new_session.user_id}, total_questions={new_session.total_questions})")
                        # Stocker l'ID de session dans la session Flask pour ce namespace utilisateur+set
                        session_key_session_id = f"quiz_session_id:{user_ns}:{rule_set.slug}"
                        session[session_key_session_id] = new_session.id
                        print(f"[QUIZ SESSION] Stored session id in flask session under key='{session_key_session_id}' -> {new_session.id}")
                    except Exception:
                        db.session.rollback()

            total_questions = len(playlist)
            index = int(session.get(playlist_index_key, 0) or 0)

            # Si terminé: fin du quiz
            if index >= total_questions:
                # Récupérer le nombre de bonnes réponses depuis la session
                total_correct_answers = int(session.get(correct_answers_session_key, 0) or 0)
                total_score = int(session.get(score_session_key, 0) or 0)
                total_questions = len(playlist)

                perfect_bonus_added = False
                perfect_bonus_value = 0
                if rule_set and rule_set.perfect_quiz_bonus and perfect_session_key:
                    perfect_bonus_value = int(rule_set.perfect_quiz_bonus or 0)
                    is_perfect = total_questions > 0 and total_correct_answers == total_questions
                    already_awarded = bool(session.get(perfect_session_key))
                    if is_perfect and perfect_bonus_value > 0 and not already_awarded:
                        total_score += perfect_bonus_value
                        session[score_session_key] = total_score
                        session[perfect_session_key] = True
                        perfect_bonus_added = True
                        if breakdown_session_key:
                            bonus_event = {
                                'type': 'perfect_bonus',
                                'label': 'Bonus quiz parfait',
                                'value': perfect_bonus_value,
                                'total_awarded': perfect_bonus_value,
                            }
                            _append_score_breakdown(breakdown_session_key, bonus_event)
                score_breakdown = list(session.get(breakdown_session_key, [])) if breakdown_session_key else []

                # Clore la UserQuizSession comme completed si présente
                if getattr(g, 'current_user', None):
                    try:
                        session_key_session_id = f"quiz_session_id:{user_ns}:{rule_set.slug}"
                        sess_id = session.get(session_key_session_id)
                        if not sess_id:
                            print(f"[QUIZ SESSION] No session id found in flask session for key='{session_key_session_id}' during quiz completion.")
                        if sess_id:
                            s = UserQuizSession.query.get(sess_id)
                            if s and s.status == 'in_progress':
                                print(
                                    f"[QUIZ SESSION] Updating session {s.id} (user={s.user_id}) before marking completed: "
                                    f"answered={s.answered_count}, total={s.total_questions}, correct={s.correct_count}, score={s.total_score}"
                                )
                                s.status = 'completed'
                                s.answered_count = s.total_questions
                                s.correct_count = total_correct_answers
                                s.total_score = total_score
                                s.updated_at = datetime.utcnow()
                                db.session.commit()
                                print(
                                    f"[QUIZ SESSION] Session {s.id} marked completed at quiz end: "
                                    f"answered={s.answered_count}, correct={s.correct_count}, score={s.total_score}"
                                )
                            else:
                                print(f"[QUIZ SESSION] Expected in-progress session for sess_id={sess_id}, found status={s.status if s else 'missing'} (user={g.current_user.id}).")
                    except Exception:
                        db.session.rollback()
                
                # Si perfect bonus obtenu, afficher l'animation d'abord
                if perfect_bonus_added:
                    return render_template(
                        'quiz_perfect_animation.html',
                        rule_set=rule_set,
                        total_questions=total_questions,
                        total_correct_answers=total_correct_answers,
                        perfect_bonus_value=perfect_bonus_value,
                        history=history_raw or ''
                    )
                
                return render_template(
                    'quiz_final.html',
                    rule_set=rule_set,
                    total_questions=total_questions,
                    total_score=total_score,
                    total_correct_answers=total_correct_answers,
                    perfect_bonus_added=perfect_bonus_added,
                    perfect_bonus_value=perfect_bonus_value,
                    score_breakdown=score_breakdown,
                    history=history_raw or '',
                    quick_double_click=quick_double_click
                )

            # Charger la prochaine question via l'ID de la playlist
            next_question_id = playlist[index]
            question = Question.query.options(
                db.joinedload(Question.images),
                db.joinedload(Question.detailed_answer_image),
                db.joinedload(Question.answer_image_links).joinedload(AnswerImageLink.image)
            ).get(next_question_id)
        else:
            # Mode sans set explicite: fallback à l'aléatoire historique (comme avant)
            query = Question.query.filter(Question.is_published.is_(True))
            query = _apply_quiz_filters(query, params)
            if history_ids:
                query = query.filter(~Question.id.in_(history_ids))
            question = query.options(
                db.joinedload(Question.images),
                db.joinedload(Question.detailed_answer_image),
                db.joinedload(Question.answer_image_links).joinedload(AnswerImageLink.image)
            ).order_by(db.func.random()).first()

        # Si on sort du mode set (pas de rule_set), marquer toute session in_progress comme abandonnée
        if not rule_set and getattr(g, 'current_user', None):
            try:
                # Abandonner toutes sessions en cours (tous sets) si l'utilisateur a quitté le set
                in_prog = UserQuizSession.query.filter_by(user_id=g.current_user.id, status='in_progress').all()
                for s in in_prog:
                    print(f"[QUIZ SESSION] Abandon session {s.id} after leaving rule_set context in /api/quiz/next (user={s.user_id})")
                    s.status = 'abandoned'
                    s.updated_at = datetime.utcnow()
                if in_prog:
                    db.session.commit()
            except Exception:
                db.session.rollback()

        # Debug logging
        print(f"[QUIZ NEXT] Rule set: {rule_set_slug}, History: {history_raw}")
        print(f"[QUIZ NEXT] Selected question ID: {question.id if question else 'None'}")
        print(f"[QUIZ NEXT] Question difficulty: {question.difficulty_level if question else 'N/A'}")

        # Calculer la progression et le score total (stocké en session)
        total_score = 0
        current_question_num = 0

        if rule_set:
            # Gestion du score en session (reset en début de session)
            if not history_raw:
                # Note: la playlist réinitialise déjà score/correct au moment de la génération
                session[score_session_key] = session.get(score_session_key, 0) or 0
                session[correct_answers_session_key] = session.get(correct_answers_session_key, 0) or 0
            total_score = int(session.get(score_session_key, 0) or 0)

            # Progression basée sur la playlist
            playlist = session.get(playlist_session_key) or []
            index = int(session.get(playlist_index_key, 0) or 0)
            # Affichage utilisateur: index courant (1-based)
            current_question_num = min(index + 1, len(playlist)) if playlist else 1
            total_questions = len(playlist)

        # Mélanger les propositions de réponses pour éviter que la bonne réponse soit toujours à la même position
        if question and question.possible_answers:
            try:
                original_answers = question.possible_answers.split('|||')
                num_answers = len(original_answers)

                # Vérifications de sécurité
                if num_answers == 0:
                    print(f"[QUIZ SHUFFLE] Question {question.id} has no answers, skipping shuffle")
                    return

                # Convertir correct_answer en int si c'est une chaîne
                try:
                    correct_answer_int = int(question.correct_answer)
                    if correct_answer_int < 1 or correct_answer_int > num_answers:
                        print(f"[QUIZ SHUFFLE] Question {question.id} has invalid correct_answer: {question.correct_answer} (should be 1-{num_answers}), skipping shuffle")
                        return
                    else:
                        question.correct_answer = correct_answer_int  # Mettre à jour pour être sûr
                except (ValueError, TypeError):
                    print(f"[QUIZ SHUFFLE] Question {question.id} has invalid correct_answer type: {type(question.correct_answer)} value: {question.correct_answer}, skipping shuffle")
                    return

                # Créer une liste d'indices [0, 1, 2, ...] et la mélanger
                answer_indices = list(range(num_answers))
                random.shuffle(answer_indices)

                # Créer les réponses dans l'ordre mélangé
                shuffled_answers = [original_answers[i] for i in answer_indices]

                # Stocker l'ordre de mélange en session pour cette question (clé par question_id)
                shuffle_key = f"question_shuffle_{question.id}"
                session[shuffle_key] = answer_indices

                # Remplacer temporairement les réponses dans l'objet question pour le template
                question._shuffled_answers = shuffled_answers

                # Calculer la nouvelle position de la bonne réponse (1-based pour correspondre à correct_answer)
                original_correct_index = question.correct_answer - 1  # 0-based
                new_correct_position = answer_indices.index(original_correct_index) + 1  # 1-based
                question._shuffled_correct_answer = new_correct_position

                # Calculer les indices originaux pour chaque position mélangée (pour les images)
                question._original_indices = answer_indices

                print(f"[QUIZ SHUFFLE] Question {question.id}: shuffled {num_answers} answers, correct answer moved from position {question.correct_answer} to {new_correct_position}")
            except Exception as e:
                print(f"[QUIZ SHUFFLE] Error shuffling answers for question {question.id}: {str(e)}, skipping shuffle")
                # En cas d'erreur, on continue sans mélanger

        return render_template('quiz_question.html',
                             question=question,
                             history=history_raw,
                             rule_set=rule_set,
                             current_question_num=current_question_num,
                             total_questions=total_questions,
                             total_score=total_score,
                             quick_double_click=quick_double_click)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/quiz/final')
def show_quiz_final():
    """Affiche le récapitulatif final du quiz (utilisé après l'animation perfect)."""
    try:
        params = request.args
        rule_set_slug = (params.get('rule_set') or '').strip()
        history_raw = (params.get('history') or '').strip()
        
        rule_set = None
        if rule_set_slug:
            rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()
        
        if not rule_set:
            return "Set de règles introuvable", 404
        
        (
            playlist_session_key,
            playlist_index_key,
            score_session_key,
            correct_answers_session_key,
            breakdown_session_key,
            streak_session_key,
            perfect_session_key,
            user_ns,
        ) = _quiz_session_keys(rule_set.slug)
        
        total_correct_answers = int(session.get(correct_answers_session_key, 0) or 0)
        total_score = int(session.get(score_session_key, 0) or 0)
        playlist = session.get(playlist_session_key) or []
        total_questions = len(playlist)
        score_breakdown = list(session.get(breakdown_session_key, [])) if breakdown_session_key else []
        perfect_bonus_added = bool(session.get(perfect_session_key))
        perfect_bonus_value = int(rule_set.perfect_quiz_bonus or 0) if perfect_bonus_added else 0
        
        quick_double_click = bool(session.get('quick_double_click_enabled', False))
        
        return render_template(
            'quiz_final.html',
            rule_set=rule_set,
            total_questions=total_questions,
            total_score=total_score,
            total_correct_answers=total_correct_answers,
            perfect_bonus_added=perfect_bonus_added,
            perfect_bonus_value=perfect_bonus_value,
            score_breakdown=score_breakdown,
            history=history_raw or '',
            quick_double_click=quick_double_click
        )
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/quiz/cancel', methods=['POST'])
def cancel_quiz_session():
    """Marque la session de quiz en cours comme abandonnée pour l'utilisateur connecté et le set fourni."""
    try:
        if not getattr(g, 'current_user', None):
            return "Non connecté", 401
        rule_set_slug = (request.form.get('rule_set') or '').strip()
        if not rule_set_slug:
            return "Paramètre 'rule_set' manquant", 400
        rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()
        if not rule_set:
            return "Set inconnu", 404
        _, _, _, _, _, _, _, user_ns = _quiz_session_keys(rule_set.slug)
        session_key_session_id = f"quiz_session_id:{user_ns}:{rule_set.slug}"
        sess_id = session.get(session_key_session_id)
        if not sess_id:
            return "Aucune session en cours", 200
        s = UserQuizSession.query.get(sess_id)
        if s and s.status == 'in_progress':
            print(f"[QUIZ SESSION] Cancel request abandoning session {s.id} for rule_set {rule_set.id} (user={s.user_id})")
            s.status = 'abandoned'
            s.updated_at = datetime.utcnow()
            db.session.commit()
        return "OK", 200
    except Exception as e:
        db.session.rollback()
        return { 'error': str(e) }, 400


def _calculate_score(rule_set, question, is_correct):
    """Calcule le score de la question et retourne le détail du calcul."""
    breakdown = {
        'type': 'question',
        'question_id': question.id if question else None,
        'question_label': (question.question_text[:120] + '…') if (question and question.question_text and len(question.question_text) > 120) else (question.question_text if question else ''),
        'difficulty': question.difficulty_level if question else None,
        'was_correct': bool(is_correct),
        'base_points': rule_set.scoring_base_points if rule_set and rule_set.scoring_base_points is not None else 0,
        'difficulty_bonus': 0,
        'difficulty_multiplier': 1.0,
        'question_points': 0,
        'combo_bonus': 0,
        'total_awarded': 0,
        'combo_streak': 0,
        'question_index': None,
    }

    if not rule_set or not is_correct:
        return 0, breakdown

    base_points = breakdown['base_points']
    points = base_points

    if rule_set.scoring_difficulty_bonus_type == 'add':
        bonus_map = rule_set.get_difficulty_bonus_map()
        bonus = bonus_map.get(str(question.difficulty_level), 0) if question else 0
        breakdown['difficulty_bonus'] = bonus
        points += bonus
    elif rule_set.scoring_difficulty_bonus_type == 'mult':
        coeff_map = rule_set.get_difficulty_bonus_map()
        coeff = coeff_map.get(str(question.difficulty_level), 1.0) if question else 1.0
        try:
            coeff = float(coeff)
        except (TypeError, ValueError):
            coeff = 1.0
        points = int(round(base_points * coeff))
        breakdown['difficulty_multiplier'] = coeff
        breakdown['difficulty_bonus'] = points - base_points

    breakdown['question_points'] = int(points)
    breakdown['total_awarded'] = int(points)

    return int(points), breakdown


@app.route('/api/debug/quiz-questions')
def debug_quiz_questions():
    """Route de debug pour afficher toutes les questions disponibles pour un quiz."""
    try:
        params = request.args
        rule_set_slug = (params.get('rule_set') or '').strip()
        history_raw = (params.get('history') or '').strip()
        history_ids = []
        if history_raw:
            for token in history_raw.split(','):
                token = token.strip()
                if token.isdigit():
                    history_ids.append(int(token))

        # Construire la requête identique à /api/quiz/next
        query = Question.query.filter(Question.is_published.is_(True))
        query = _apply_quiz_filters(query, params)

        if history_ids:
            query = query.filter(~Question.id.in_(history_ids))

        # Exclure questions déjà vues par l'utilisateur connecté
        if getattr(g, 'current_user', None):
            seen_ids = [row.question_id for row in UserQuestionStat.query.with_entities(UserQuestionStat.question_id).filter_by(user_id=g.current_user.id).all()]
            if seen_ids:
                query = query.filter(~Question.id.in_(seen_ids))

        # Appliquer la logique de set de règles si présent
        rule_set = None
        selected_diff = None
        if rule_set_slug:
            rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()

        if rule_set and rule_set.get_questions_per_difficulty():
            # Logique de quotas par difficulté
            qmap = rule_set.get_questions_per_difficulty()
            allowed_diffs = rule_set.get_allowed_difficulties() or [1, 2, 3, 4, 5]

            # Compter les questions déjà posées par difficulté dans cette session
            history_questions = []
            if history_ids:
                history_questions = Question.query.filter(Question.id.in_(history_ids)).all()

            diff_counts = {d: sum(1 for q in history_questions if q.difficulty_level == d) for d in allowed_diffs}

            # Trouver les difficultés qui n'ont pas atteint leur quota
            available_diffs = []
            for d in allowed_diffs:
                max_q = qmap.get(str(d), 0)
                current_q = diff_counts.get(d, 0)
                if current_q < max_q:
                    available_diffs.append(d)

            if available_diffs:
                # Trier les difficultés par ordre croissant (1, 2, 3, 4, 5)
                available_diffs_sorted = sorted(available_diffs)
                # Sélectionner la difficulté la plus basse disponible
                selected_diff = available_diffs_sorted[0]
                query = query.filter(Question.difficulty_level == selected_diff)

        # Récupérer toutes les questions disponibles (sans random)
        questions = query.options(
            db.joinedload(Question.broad_theme),
            db.joinedload(Question.specific_theme),
            db.joinedload(Question.countries)
        ).order_by(Question.id).all()

        # Préparer les données de debug
        debug_data = {
            'rule_set': rule_set.name if rule_set else None,
            'rule_set_slug': rule_set_slug,
            'history_ids': history_ids,
            'selected_difficulty': selected_diff,
            'total_available_questions': len(questions),
            'questions': []
        }

        for q in questions:
            question_data = {
                'id': q.id,
                'question_text': q.question_text[:100] + '...' if len(q.question_text) > 100 else q.question_text,
                'difficulty_level': q.difficulty_level,
                'correct_answer': q.correct_answer,
                'broad_theme': q.broad_theme.name if q.broad_theme else None,
                'specific_theme': q.specific_theme.name if q.specific_theme else None,
                'countries': [c.name for c in q.countries] if q.countries else [],
                'times_answered': q.times_answered,
                'success_count': q.success_count
            }
            debug_data['questions'].append(question_data)

        # Afficher aussi dans la console du serveur
        print(f"\n=== DEBUG QUIZ QUESTIONS ===")
        print(f"Rule set: {debug_data['rule_set']} ({rule_set_slug})")
        print(f"History IDs: {history_ids}")
        print(f"Selected difficulty: {selected_diff}")
        print(f"Total available questions: {len(questions)}")
        print(f"Questions: {[q['id'] for q in debug_data['questions']]}")
        print("===========================\n")

        return debug_data

    except Exception as e:
        return {'error': str(e)}, 400


@app.route('/api/quiz/answer', methods=['POST'])
def submit_quiz_answer():
    """Valider la réponse de l'utilisateur, mettre à jour les stats et retourner le résultat."""
    try:
        question_id_raw = (request.form.get('question_id') or '').strip()
        selected_answer = (request.form.get('selected_answer') or '').strip()
        history_raw = (request.form.get('history') or '').strip()
        rule_set_slug = (request.form.get('rule_set') or '').strip()
        is_timeout = bool((request.form.get('timeout') or '').strip())
        quick_double_click_raw = request.form.get('quick_double_click')
        if quick_double_click_raw is not None:
            quick_double_click = quick_double_click_raw.strip().lower() == 'true'
            session['quick_double_click_enabled'] = quick_double_click
        elif 'quick_double_click_enabled' in session:
            quick_double_click = bool(session.get('quick_double_click_enabled'))
        else:
            quick_double_click = _get_user_double_click_preference()
            session['quick_double_click_enabled'] = quick_double_click

        if not question_id_raw.isdigit():
            return "Identifiant de question invalide", 400

        question = Question.query.options(
            db.joinedload(Question.images),
            db.joinedload(Question.detailed_answer_image),
            db.joinedload(Question.answer_image_links).joinedload(AnswerImageLink.image)
        ).get_or_404(int(question_id_raw))

        # Vérifier si les réponses ont été mélangées pour cette question
        shuffle_key = f"question_shuffle_{question.id}"
        shuffle_order = session.get(shuffle_key)

        if shuffle_order and selected_answer and selected_answer.isdigit():
            # Convertir l'index sélectionné (dans l'ordre mélangé, 1-based) vers l'index original (1-based)
            selected_index_mixed = int(selected_answer) - 1  # 0-based
            original_index = shuffle_order[selected_index_mixed] + 1  # 1-based
            selected_answer_original = str(original_index)
        else:
            selected_answer_original = selected_answer

        correct_value = (question.correct_answer or '').strip()
        # Si pas de réponse (timer expiré ou non sélection), considérer comme faux
        is_correct = bool(selected_answer_original) and (selected_answer_original == correct_value)

        # Debug logging
        print(f"[QUIZ ANSWER] Question ID: {question_id_raw}, Selected: '{selected_answer}', Correct: '{correct_value}', Is correct: {is_correct}")

        # Charger le set de règles si spécifié
        rule_set = None
        playlist_session_key = playlist_index_key = score_session_key = correct_answers_session_key = breakdown_session_key = streak_session_key = perfect_session_key = user_ns = None
        if rule_set_slug:
            rule_set = QuizRuleSet.query.filter_by(slug=rule_set_slug, is_active=True).first()
            if rule_set:
                (
                    playlist_session_key,
                    playlist_index_key,
                    score_session_key,
                    correct_answers_session_key,
                    breakdown_session_key,
                    streak_session_key,
                    perfect_session_key,
                    user_ns,
                ) = _quiz_session_keys(rule_set.slug)

        # Calculer le score selon les règles
        score = 0
        breakdown = None
        combo_triggered = False
        combo_bonus = 0
        combo_streak = 0
        if rule_set:
            history_ids = []
            if history_raw:
                for token in history_raw.split(','):
                    token = token.strip()
                    if token.isdigit():
                        history_ids.append(int(token))
            question_index = len(history_ids) + 1
            question_score, breakdown = _calculate_score(rule_set, question, is_correct)

            streak_after = 0
            if rule_set.combo_bonus_enabled and rule_set.combo_step and rule_set.combo_bonus_points:
                combo_step = max(int(rule_set.combo_step), 0)
                combo_points = int(rule_set.combo_bonus_points or 0)
                current_streak = int(session.get(streak_session_key, 0) or 0) if streak_session_key else 0
                if is_correct and combo_step > 0 and combo_points > 0:
                    current_streak += 1
                    if current_streak % combo_step == 0:
                        combo_bonus = combo_points
                        combo_triggered = True
                else:
                    current_streak = 0
                streak_after = current_streak
                combo_streak = streak_after
                if streak_session_key:
                    session[streak_session_key] = current_streak
            else:
                if streak_session_key:
                    session[streak_session_key] = 0

            if breakdown:
                breakdown['question_index'] = question_index
                breakdown['combo_bonus'] = combo_bonus
                breakdown['combo_triggered'] = combo_triggered
                breakdown['combo_streak'] = streak_after
                breakdown['total_awarded'] = int(breakdown.get('question_points', 0) + combo_bonus)
                score = breakdown['total_awarded']
            else:
                score = question_score + combo_bonus

            if breakdown_session_key and breakdown:
                _append_score_breakdown(breakdown_session_key, breakdown)

        # Mettre à jour les statistiques globales de la question
        question.times_answered = (question.times_answered or 0) + 1
        if is_correct:
            question.success_count = (question.success_count or 0) + 1
        question.updated_at = datetime.utcnow()

        # Mettre à jour les statistiques utilisateur-question
        if getattr(g, 'current_user', None):
            stat = UserQuestionStat.query.filter_by(user_id=g.current_user.id, question_id=question.id).first()
            if not stat:
                stat = UserQuestionStat(user_id=g.current_user.id, question_id=question.id)
                db.session.add(stat)
            stat.times_answered = (stat.times_answered or 0) + 1
            if is_correct:
                stat.success_count = (stat.success_count or 0) + 1
            stat.last_selected_answer = selected_answer_original
            stat.last_is_correct = is_correct
            stat.last_answered_at = datetime.utcnow()

        # Mettre à jour la distribution des réponses (QuestionAnswerStat)
        try:
            if selected_answer_original and selected_answer_original.isdigit():
                idx = int(selected_answer_original)
                qa = QuestionAnswerStat.query.filter_by(question_id=question.id, answer_index=idx).first()
                if not qa:
                    qa = QuestionAnswerStat(question_id=question.id, answer_index=idx, selected_count=0)
                    db.session.add(qa)
                qa.selected_count = (qa.selected_count or 0) + 1
        except Exception:
            # Ne pas bloquer la réponse si l'agg échoue
            db.session.rollback()

        db.session.commit()

        # Mettre à jour le score total et le nombre de bonnes réponses en session (namespace user)
        if rule_set and score_session_key and correct_answers_session_key:
            total_score_session = int(session.get(score_session_key, 0) or 0)
            if score:
                total_score_session += int(score)
            session[score_session_key] = total_score_session

            # Compter les bonnes réponses
            total_correct_answers_session = int(session.get(correct_answers_session_key, 0) or 0)
            if is_correct:
                total_correct_answers_session += 1
            session[correct_answers_session_key] = total_correct_answers_session

        # Mettre à jour la progression de playlist (si set de règles, namespace user)
        if rule_set and playlist_session_key and playlist_index_key:
            index = int(session.get(playlist_index_key, 0) or 0)
            playlist = session.get(playlist_session_key) or []
            # Avancer l'index si la question correspond à l'élément courant
            if index < len(playlist) and playlist[index] == question.id:
                session[playlist_index_key] = index + 1

            # Mettre à jour la UserQuizSession si présente
            if getattr(g, 'current_user', None):
                try:
                    session_key_session_id = f"quiz_session_id:{user_ns}:{rule_set.slug}"
                    sess_id = session.get(session_key_session_id)
                    if not sess_id:
                        print(f"[QUIZ SESSION] No session id found in flask session for key='{session_key_session_id}' during answer update.")
                    if sess_id:
                        s = UserQuizSession.query.get(sess_id)
                        if s and s.status == 'in_progress':
                            before_answered = s.answered_count or 0
                            before_correct = s.correct_count or 0
                            before_score = s.total_score or 0
                            print(
                                f"[QUIZ SESSION] Answer update for session {s.id} (user={s.user_id}): "
                                f"answered={before_answered}, total={s.total_questions}, correct={before_correct}, score={before_score}"
                            )
                            s.answered_count = min((s.answered_count or 0) + 1, s.total_questions or 0)
                            if is_correct:
                                s.correct_count = (s.correct_count or 0) + 1
                            # total_score est déjà mis à jour en session; l'appliquer si on a un score crédité
                            if score:
                                s.total_score = (s.total_score or 0) + int(score)
                            if (s.total_questions or 0) > 0 and s.answered_count >= (s.total_questions or 0):
                                s.status = 'completed'
                                print(f"[QUIZ SESSION] Session {s.id} reached completion via answer handler.")
                            s.updated_at = datetime.utcnow()
                            db.session.commit()
                            print(
                                f"[QUIZ SESSION] Post-answer session {s.id}: "
                                f"status={s.status}, answered={s.answered_count}, correct={s.correct_count}, score={s.total_score}"
                            )
                        else:
                            print(f"[QUIZ SESSION] Retrieved session {getattr(s, 'id', None)} but status={getattr(s, 'status', None)} during answer update (expected in_progress).")
                except Exception:
                    db.session.rollback()

        # Mettre à jour l'historique côté client (ajouter la question actuelle)
        history_ids = []
        if history_raw:
            for token in history_raw.split(','):
                token = token.strip()
                if token.isdigit():
                    history_ids.append(int(token))
        if question.id not in history_ids:
            history_ids.append(question.id)
        next_history = ','.join(str(i) for i in history_ids)

        # Calculer la progression et le score total mis à jour
        total_questions = 0
        current_question_num = 0
        total_score = 0

        if rule_set:
            # Progression basée sur la playlist
            (
                playlist_session_key,
                playlist_index_key,
                score_session_key,
                correct_answers_session_key,
                breakdown_session_key,
                streak_session_key,
                perfect_session_key,
                user_ns,
            ) = _quiz_session_keys(rule_set.slug)
            index = int(session.get(playlist_index_key, 0) or 0)
            playlist = session.get(playlist_session_key) or []
            total_questions = len(playlist)
            current_question_num = min(index, total_questions)

            # Score total depuis la session
            score_session_key = score_session_key
            total_score = int(session.get(score_session_key, 0) or 0)

        return render_template(
            'quiz_result.html',
            question=question,
            is_correct=is_correct,
            selected=selected_answer_original,
            history=next_history,
            rule_set=rule_set,
            score=score,
            combo_triggered=combo_triggered,
            combo_bonus=combo_bonus,
            combo_streak=combo_streak,
            current_question_num=current_question_num,
            total_questions=total_questions,
            total_score=total_score,
            is_timeout=is_timeout,
            quick_double_click=quick_double_click
        )
    except Exception as e:
        return f"Erreur: {str(e)}", 400


# ============ Routes pour la gestion des règles du Quiz ============

def _slugify(value: str) -> str:
    value = (value or '').strip().lower()
    safe = []
    for ch in value:
        if ch.isalnum():
            safe.append(ch)
        elif ch in [' ', '-', '_']:
            safe.append('-')
    slug = ''.join(safe)
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-')


@app.route('/quiz-rules')
def quiz_rules_page():
    """Page d'administration des ensembles de règles du quiz"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    return render_template('quiz_rules.html')


@app.route('/api/quiz-rules')
def list_quiz_rules():
    """Retourner la liste des sets de règles en HTML (pour HTMX)"""
    denied = _ensure_perm_api()
    if denied:
        return denied
    rules = QuizRuleSet.query.order_by(QuizRuleSet.updated_at.desc()).all()
    return render_template('quiz_rules_list.html', rules=rules)


@app.route('/quiz-rule/<int:rule_id>/stats')
def quiz_rule_stats_page(rule_id: int):
    """Page admin des statistiques d'un set de règles."""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    rule = QuizRuleSet.query.get_or_404(rule_id)

    # Sessions liées à ce set
    q_sessions = UserQuizSession.query.filter_by(rule_set_id=rule.id).all()
    total_played = len(q_sessions)
    completed_sessions = [s for s in q_sessions if s.status == 'completed']
    abandoned_sessions = [s for s in q_sessions if s.status == 'abandoned']
    total_completed = len(completed_sessions)

    # Scores
    scores = [s.total_score or 0 for s in completed_sessions]
    avg_score = (sum(scores) / len(scores)) if scores else 0.0
    best_score = max(scores) if scores else 0
    worst_score = min(scores) if scores else 0

    # Bonnes réponses moyennes
    corrects = [s.correct_count or 0 for s in completed_sessions]
    avg_correct = (sum(corrects) / len(corrects)) if corrects else 0.0

    # Joueurs et nombre de sessions jouées
    from collections import Counter
    user_counts = Counter([s.user_id for s in q_sessions if s.user_id])
    players = []
    if user_counts:
        users = User.query.filter(User.id.in_(list(user_counts.keys()))).all()
        id_to_user = {u.id: u for u in users}
        players = [{'user': id_to_user.get(uid), 'count': cnt} for uid, cnt in user_counts.items()]
        # Trier par nombre décroissant
        players.sort(key=lambda x: x['count'], reverse=True)

    return render_template('quiz_rule_stats.html',
                           rule=rule,
                           total_played=total_played,
                           total_completed=total_completed,
                           total_abandoned=len(abandoned_sessions),
                           avg_score=avg_score,
                           best_score=best_score,
                           worst_score=worst_score,
                           avg_correct=avg_correct,
                           players=players)


def _load_quiz_rule_defaults():
    """Charger les valeurs par défaut depuis le fichier JSON"""
    defaults_path = os.path.join(os.path.dirname(__file__), 'config', 'quiz_rules_defaults.json')
    try:
        if os.path.exists(defaults_path):
            with open(defaults_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config.get('defaults', {})
    except Exception as e:
        print(f"Erreur lors du chargement des valeurs par défaut: {e}")
    
    # Valeurs par défaut en dur si le fichier n'existe pas
    return {
        'is_active': True,
        'timer_seconds': 30,
        'use_all_broad_themes': True,
        'use_all_specific_themes': True,
        'check_all_broad_themes': True,
        'check_all_specific_themes': True,
        'allowed_difficulties': [1, 2, 3, 4, 5],
        'questions_per_difficulty': {'1': 2, '2': 3, '3': 3, '4': 2, '5': 1},
        'scoring_base_points': 10,
        'scoring_difficulty_bonus_type': 'add',
        'difficulty_bonus_map': {'1': 0, '2': 5, '3': 10, '4': 15, '5': 20},
        'combo_bonus_enabled': True,
        'combo_step': 3,
        'combo_bonus_points': 5,
        'perfect_quiz_bonus': 50,
        'intro_message': 'Bonne chance ! 🍀',
        'success_message': 'Félicitations ! 🎉',
        'question_order_mode': 'difficulty_ascending'
    }


@app.route('/quiz-rule/new')
def new_quiz_rule():
    """Formulaire pour créer un nouveau set de règles"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    if not _has_perm('can_create_rule'):
        return _deny_access("Permission 'can_create_rule' requise")
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    specific_themes = SpecificTheme.query.join(BroadTheme).order_by(BroadTheme.name, SpecificTheme.name).all()
    countries = Country.query.order_by(Country.name).all()
    images = ImageAsset.query.order_by(ImageAsset.title).all()
    
    # Charger les valeurs par défaut
    defaults = _load_quiz_rule_defaults()

    return render_template('quiz_rule_form.html', rule=None, themes=themes, specific_themes=specific_themes, countries=countries, images=images, defaults=defaults)


@app.route('/quiz-rule/<int:rule_id>/edit')
def edit_quiz_rule(rule_id: int):
    """Formulaire pour éditer un set de règles existant"""
    resp = _ensure_admin_page_redirect()
    if resp:
        return resp
    rule = QuizRuleSet.query.get_or_404(rule_id)
    can_any = _has_perm('can_update_delete_any_rule')
    can_own = _has_perm('can_update_delete_own_rule')
    if not (can_any or (can_own and getattr(g, 'current_user', None) and rule.created_by_user_id == g.current_user.id)):
        return _deny_access("Permission 'can_update_delete_own_rule' ou 'can_update_delete_any_rule' requise")
    themes = BroadTheme.query.order_by(BroadTheme.name).all()
    specific_themes = SpecificTheme.query.join(BroadTheme).order_by(BroadTheme.name, SpecificTheme.name).all()
    countries = Country.query.order_by(Country.name).all()
    images = ImageAsset.query.order_by(ImageAsset.title).all()
    return render_template('quiz_rule_form.html', rule=rule, themes=themes, specific_themes=specific_themes, countries=countries, images=images, defaults={})


@app.route('/api/quiz-rule', methods=['POST'])
def create_quiz_rule():
    """Créer un nouveau set de règles"""
    try:
        denied = _ensure_perm_api('can_create_rule')
        if denied:
            return denied
        data = request.form

        name = (data.get('name') or '').strip()
        if not name:
            return "Nom requis", 400

        slug = (data.get('slug') or '').strip() or _slugify(name)
        order_mode = (data.get('question_order_mode') or 'difficulty_ascending').strip() or 'difficulty_ascending'
        if order_mode not in ['difficulty_ascending', 'full_shuffle']:
            order_mode = 'difficulty_ascending'

        created_by_user_id = g.current_user.id if getattr(g, 'current_user', None) else None
        rule = QuizRuleSet(
            name=name,
            slug=slug,
            description=(data.get('description') or '').strip() or None,
            comment=(data.get('comment') or '').strip() or None,
            is_active=(data.get('is_active') == 'on'),
            created_by_user_id=created_by_user_id,
            timer_seconds=int(data.get('timer_seconds') or 30),
            use_all_countries=(data.get('use_all_countries') == 'on'),
            use_all_broad_themes=(data.get('use_all_broad_themes') == 'on'),
            use_all_specific_themes=(data.get('use_all_specific_themes') == 'on'),
            scoring_base_points=int(data.get('scoring_base_points') or 1),
            scoring_difficulty_bonus_type=(data.get('scoring_difficulty_bonus_type') or 'none'),
            combo_bonus_enabled=(data.get('combo_bonus_enabled') == 'on'),
            combo_step=(int(data.get('combo_step')) if data.get('combo_step') else None),
            combo_bonus_points=(int(data.get('combo_bonus_points')) if data.get('combo_bonus_points') else None),
            perfect_quiz_bonus=int(data.get('perfect_quiz_bonus') or 0),
            min_correct_answers_to_win=int(data.get('min_correct_answers_to_win') or 0),
            intro_message=(data.get('intro_message') or '').strip() or None,
            success_message=(data.get('success_message') or '').strip() or None,
            failure_message=(data.get('failure_message') or '').strip() or None,
            intro_image_id=(int(data.get('intro_image_id')) if (data.get('intro_image_id') or '').isdigit() else None),
            success_image_id=(int(data.get('success_image_id')) if (data.get('success_image_id') or '').isdigit() else None),
            failure_image_id=(int(data.get('failure_image_id')) if (data.get('failure_image_id') or '').isdigit() else None),
            question_order_mode=order_mode
        )

        # Difficultés autorisées
        difficulties = [int(x) for x in data.getlist('allowed_difficulties') if (x or '').isdigit()]
        rule.set_allowed_difficulties(difficulties)

        # Quotas par difficulté
        quotas = {}
        for d in range(1, 6):
            val = (data.get(f'questions_per_difficulty_{d}') or '').strip()
            if val.isdigit():
                quotas[str(d)] = int(val)
        rule.set_questions_per_difficulty(quotas)

        # Bonus selon difficulté
        bonus_map = {}
        for d in range(1, 6):
            raw = (data.get(f'difficulty_bonus_{d}') or '').strip()
            if raw:
                try:
                    bonus_map[str(d)] = float(raw)
                except Exception:
                    pass
        rule.set_difficulty_bonus_map(bonus_map)

        # Pays autorisés (si non tous)
        if not rule.use_all_countries:
            ids = [int(x) for x in data.getlist('allowed_country_ids') if (x or '').isdigit()]
            if ids:
                rule.allowed_countries = Country.query.filter(Country.id.in_(ids)).all()

        # Thèmes autorisés (si non tous)
        if not rule.use_all_broad_themes:
            ids = [int(x) for x in data.getlist('allowed_broad_theme_ids') if (x or '').isdigit()]
            if ids:
                rule.allowed_broad_themes = BroadTheme.query.filter(BroadTheme.id.in_(ids)).all()

        if not rule.use_all_specific_themes:
            ids = [int(x) for x in data.getlist('allowed_specific_theme_ids') if (x or '').isdigit()]
            if ids:
                rule.allowed_specific_themes = SpecificTheme.query.filter(SpecificTheme.id.in_(ids)).all()

        # Détection automatique du mode de sélection
        selected_question_ids = [int(x) for x in data.getlist('selected_question_ids') if (x or '').isdigit()]
        
        # Récupérer toutes les questions disponibles selon les critères
        available_question_ids = []
        if not rule.use_all_specific_themes and rule.allowed_specific_themes:
            specific_theme_ids = [st.id for st in rule.allowed_specific_themes]
        else:
            specific_theme_ids = [st.id for st in SpecificTheme.query.all()]
        
        if specific_theme_ids and difficulties:
            available_questions = Question.query.filter(
                Question.specific_theme_id.in_(specific_theme_ids),
                Question.difficulty_level.in_(difficulties)
            ).all()
            available_question_ids = [q.id for q in available_questions]
        
        # Déterminer le mode : si toutes les questions disponibles sont sélectionnées -> mode auto
        # sinon -> mode manuel
        if selected_question_ids and set(selected_question_ids) != set(available_question_ids):
            # Mode manuel : l'utilisateur a désélectionné des questions
            rule.question_selection_mode = 'manual'
            rule.selected_questions = Question.query.filter(Question.id.in_(selected_question_ids)).all()
        else:
            # Mode auto : toutes les questions sont sélectionnées
            rule.question_selection_mode = 'auto'
            rule.selected_questions = []  # Vider la liste en mode auto

        db.session.add(rule)
        db.session.commit()

        rules = QuizRuleSet.query.order_by(QuizRuleSet.updated_at.desc()).all()
        return render_template('quiz_rules_list.html', rules=rules)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/quiz-rule/<int:rule_id>', methods=['PUT', 'POST'])
def update_quiz_rule(rule_id: int):
    """Mettre à jour un set de règles existant"""
    try:
        rule = QuizRuleSet.query.get_or_404(rule_id)
        can_any = _has_perm('can_update_delete_any_rule')
        can_own = _has_perm('can_update_delete_own_rule')
        denied = _ensure_perm_api()
        if denied:
            return denied
        if not (can_any or (can_own and getattr(g, 'current_user', None) and rule.created_by_user_id == g.current_user.id)):
            return _deny_access("Permission 'can_update_delete_own_rule' ou 'can_update_delete_any_rule' requise")
        data = request.form

        name = (data.get('name') or '').strip()
        if name:
            rule.name = name

        slug = (data.get('slug') or '').strip()
        if slug:
            rule.slug = slug
        else:
            # si slug vide explicitement, régénérer à partir du nom
            rule.slug = _slugify(rule.name)

        rule.description = (data.get('description') or '').strip() or None
        rule.comment = (data.get('comment') or '').strip() or None
        rule.is_active = (data.get('is_active') == 'on')

        if can_any and data.get('created_by_user_id') and data.get('created_by_user_id').isdigit():
            rule.created_by_user_id = int(data.get('created_by_user_id'))

        rule.timer_seconds = int(data.get('timer_seconds') or rule.timer_seconds or 30)
        rule.use_all_countries = (data.get('use_all_countries') == 'on')
        rule.use_all_broad_themes = (data.get('use_all_broad_themes') == 'on')
        rule.use_all_specific_themes = (data.get('use_all_specific_themes') == 'on')
        rule.scoring_base_points = int(data.get('scoring_base_points') or rule.scoring_base_points or 1)
        rule.scoring_difficulty_bonus_type = (data.get('scoring_difficulty_bonus_type') or rule.scoring_difficulty_bonus_type or 'none')
        rule.combo_bonus_enabled = (data.get('combo_bonus_enabled') == 'on')
        rule.combo_step = (int(data.get('combo_step')) if data.get('combo_step') else None)
        rule.combo_bonus_points = (int(data.get('combo_bonus_points')) if data.get('combo_bonus_points') else None)
        rule.perfect_quiz_bonus = int(data.get('perfect_quiz_bonus') or rule.perfect_quiz_bonus or 0)
        rule.min_correct_answers_to_win = int(data.get('min_correct_answers_to_win') or rule.min_correct_answers_to_win or 0)
        order_mode = (data.get('question_order_mode') or rule.question_order_mode or 'difficulty_ascending').strip()
        if order_mode not in ['difficulty_ascending', 'full_shuffle']:
            order_mode = 'difficulty_ascending'
        rule.question_order_mode = order_mode

        rule.intro_message = (data.get('intro_message') or '').strip() or None
        rule.success_message = (data.get('success_message') or '').strip() or None
        rule.failure_message = (data.get('failure_message') or '').strip() or None
        rule.intro_image_id = (int(data.get('intro_image_id')) if (data.get('intro_image_id') or '').isdigit() else None)
        rule.success_image_id = (int(data.get('success_image_id')) if (data.get('success_image_id') or '').isdigit() else None)
        rule.failure_image_id = (int(data.get('failure_image_id')) if (data.get('failure_image_id') or '').isdigit() else None)

        # Difficultés autorisées
        difficulties = [int(x) for x in data.getlist('allowed_difficulties') if (x or '').isdigit()]
        rule.set_allowed_difficulties(difficulties)

        # Quotas par difficulté
        quotas = {}
        for d in range(1, 6):
            val = (data.get(f'questions_per_difficulty_{d}') or '').strip()
            if val.isdigit():
                quotas[str(d)] = int(val)
        rule.set_questions_per_difficulty(quotas)

        # Bonus selon difficulté
        bonus_map = {}
        for d in range(1, 6):
            raw = (data.get(f'difficulty_bonus_{d}') or '').strip()
            if raw:
                try:
                    bonus_map[str(d)] = float(raw)
                except Exception:
                    pass
        rule.set_difficulty_bonus_map(bonus_map)

        # Pays autorisés (si non tous)
        if rule.use_all_countries:
            rule.allowed_countries = []
        else:
            ids = [int(x) for x in data.getlist('allowed_country_ids') if (x or '').isdigit()]
            rule.allowed_countries = Country.query.filter(Country.id.in_(ids)).all() if ids else []

        # Thèmes autorisés (si non tous)
        if rule.use_all_broad_themes:
            rule.allowed_broad_themes = []
        else:
            ids = [int(x) for x in data.getlist('allowed_broad_theme_ids') if (x or '').isdigit()]
            rule.allowed_broad_themes = BroadTheme.query.filter(BroadTheme.id.in_(ids)).all() if ids else []

        if rule.use_all_specific_themes:
            rule.allowed_specific_themes = []
        else:
            ids = [int(x) for x in data.getlist('allowed_specific_theme_ids') if (x or '').isdigit()]
            rule.allowed_specific_themes = SpecificTheme.query.filter(SpecificTheme.id.in_(ids)).all() if ids else []

        # Détection automatique du mode de sélection
        selected_question_ids = [int(x) for x in data.getlist('selected_question_ids') if (x or '').isdigit()]
        
        # Récupérer toutes les questions disponibles selon les critères
        available_question_ids = []
        if not rule.use_all_specific_themes and rule.allowed_specific_themes:
            specific_theme_ids = [st.id for st in rule.allowed_specific_themes]
        else:
            specific_theme_ids = [st.id for st in SpecificTheme.query.all()]
        
        if specific_theme_ids and difficulties:
            available_questions = Question.query.filter(
                Question.specific_theme_id.in_(specific_theme_ids),
                Question.difficulty_level.in_(difficulties)
            ).all()
            available_question_ids = [q.id for q in available_questions]
        
        # Déterminer le mode : si toutes les questions disponibles sont sélectionnées -> mode auto
        # sinon -> mode manuel
        if selected_question_ids and set(selected_question_ids) != set(available_question_ids):
            # Mode manuel : l'utilisateur a désélectionné des questions
            rule.question_selection_mode = 'manual'
            rule.selected_questions = Question.query.filter(Question.id.in_(selected_question_ids)).all()
        else:
            # Mode auto : toutes les questions sont sélectionnées
            rule.question_selection_mode = 'auto'
            rule.selected_questions = []  # Vider la liste en mode auto

        rule.updated_at = datetime.utcnow()
        db.session.commit()

        rules = QuizRuleSet.query.order_by(QuizRuleSet.updated_at.desc()).all()
        return render_template('quiz_rules_list.html', rules=rules)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/quiz-rule/<int:rule_id>', methods=['DELETE'])
def delete_quiz_rule(rule_id: int):
    """Supprimer un set de règles"""
    try:
        rule = QuizRuleSet.query.get_or_404(rule_id)
        can_any = _has_perm('can_update_delete_any_rule')
        can_own = _has_perm('can_update_delete_own_rule')
        denied = _ensure_perm_api()
        if denied:
            return denied
        if not (can_any or (can_own and getattr(g, 'current_user', None) and rule.created_by_user_id == g.current_user.id)):
            return _deny_access("Permission 'can_update_delete_own_rule' ou 'can_update_delete_any_rule' requise")
        db.session.delete(rule)
        db.session.commit()
        rules = QuizRuleSet.query.order_by(QuizRuleSet.updated_at.desc()).all()
        return render_template('quiz_rules_list.html', rules=rules)
    except Exception as e:
        return f"Erreur: {str(e)}", 400


@app.route('/api/quiz-rule/check-name', methods=['GET'])
def check_quiz_rule_name():
    """Vérifier si un nom de règle existe déjà"""
    name = request.args.get('name', '').strip()
    exclude_id = request.args.get('exclude_id')

    if not name:
        return ''

    # Normaliser le nom saisi (supprimer accents et convertir en minuscules)
    normalized_input = unidecode(name).lower()

    # Récupérer tous les noms existants et les normaliser
    existing_rules = QuizRuleSet.query.all()
    for rule in existing_rules:
        # Exclure la règle actuelle en édition
        if exclude_id and str(rule.id) == exclude_id:
            continue

        # Normaliser le nom existant
        normalized_existing = unidecode(rule.name).lower()

        # Vérifier si les noms normalisés correspondent
        if normalized_input == normalized_existing:
            return f'<span class="field-error">Le nom \'{name}\' existe déjà</span>'

    # Si aucun nom similaire n'est trouvé
    return f'<span style="color: #28a745; font-size: 0.875rem;">✓ Le nom \'{name}\' est disponible</span>'


@app.route('/api/quiz-rule/check-slug', methods=['GET'])
def check_quiz_rule_slug():
    """Vérifier si un slug de règle existe déjà"""
    slug = request.args.get('slug', '').strip()
    exclude_id = request.args.get('exclude_id')

    if not slug:
        return ''

    # Vérifier si le slug existe déjà
    query = QuizRuleSet.query.filter_by(slug=slug)
    if exclude_id and exclude_id.isdigit():
        query = query.filter(QuizRuleSet.id != int(exclude_id))

    exists = query.first() is not None

    if exists:
        return f'<span class="field-error">Le slug \'{slug}\' existe déjà</span>'
    else:
        return f'<span style="color: #28a745; font-size: 0.875rem;">✓ Le slug \'{slug}\' est disponible</span>'


@app.route('/api/quiz-rule/count-questions', methods=['GET'])
def count_questions_for_rule():
    """Compter le nombre de questions disponibles selon les critères sélectionnés"""
    country_ids = request.args.getlist('country_ids[]', type=int)
    filter_by_countries = request.args.get('filter_by_countries') == '1'
    specific_theme_ids = request.args.getlist('specific_theme_ids[]', type=int)
    difficulty_levels = request.args.getlist('difficulty_levels[]', type=int)

    if not specific_theme_ids or not difficulty_levels:
        return {'count': 0, 'message': 'Sélectionnez au moins un sous-thème et une difficulté'}

    try:
        # Compter les questions qui correspondent aux critères
        query = Question.query.filter(
            Question.specific_theme_id.in_(specific_theme_ids),
            Question.difficulty_level.in_(difficulty_levels)
        )

        # Filtrer par pays si demandé
        if filter_by_countries:
            # Si filter_by_countries est présent, on filtre selon les pays sélectionnés
            if country_ids:
                # Questions qui ont au moins un des pays sélectionnés (évite les doublons)
                query = query.filter(Question.countries.any(Country.id.in_(country_ids)))
            else:
                # Aucun pays sélectionné = seulement les questions générales (sans pays)
                query = query.filter(~Question.countries.any())

        count = query.count()

        if count == 0:
            message = 'Aucune question ne correspond à ces critères'
        elif count == 1:
            message = '1 question disponible'
        else:
            message = f'{count} questions disponibles'

        return {'count': count, 'message': message}

    except Exception as e:
        print(f"Erreur lors du comptage des questions: {e}")
        return {'count': 0, 'message': 'Erreur lors du calcul'}


@app.route('/api/quiz-rule/get-questions', methods=['GET'])
def get_questions_for_selection():
    """Récupérer les questions disponibles pour la sélection manuelle"""
    country_ids = request.args.getlist('country_ids[]', type=int)
    filter_by_countries = request.args.get('filter_by_countries') == '1'
    specific_theme_ids = request.args.getlist('specific_theme_ids[]', type=int)
    difficulty_levels = request.args.getlist('difficulty_levels[]', type=int)

    if not specific_theme_ids or not difficulty_levels:
        return {'questions': [], 'message': 'Sélectionnez au moins un sous-thème et une difficulté'}

    try:
        # Récupérer les questions qui correspondent aux critères
        query = Question.query.filter(
            Question.specific_theme_id.in_(specific_theme_ids),
            Question.difficulty_level.in_(difficulty_levels)
        )

        # Filtrer par pays si demandé
        if filter_by_countries:
            # Si filter_by_countries est présent, on filtre selon les pays sélectionnés
            if country_ids:
                # Questions qui ont au moins un des pays sélectionnés (évite les doublons)
                query = query.filter(Question.countries.any(Country.id.in_(country_ids)))
            else:
                # Aucun pays sélectionné = seulement les questions générales (sans pays)
                query = query.filter(~Question.countries.any())

        questions = query.order_by(Question.specific_theme_id, Question.difficulty_level, Question.id).all()

        questions_data = []
        for q in questions:
            questions_data.append({
                'id': q.id,
                'question_text': q.question_text[:200] + '...' if len(q.question_text) > 200 else q.question_text,
                'broad_theme_name': q.theme.name if q.theme else None,
                'specific_theme_name': q.specific_theme_obj.name if q.specific_theme_obj else None,
                'difficulty_level': q.difficulty_level
            })

        return {'questions': questions_data, 'count': len(questions_data)}

    except Exception as e:
        print(f"Erreur lors de la récupération des questions: {e}")
        return {'questions': [], 'error': str(e)}


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)


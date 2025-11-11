"""
Script de migration pour ajouter l'option d'ordre des questions aux QuizRuleSet.

Ce script ajoute :
1. Une colonne `question_order_mode` (VARCHAR(30)) avec la valeur par défaut
   `difficulty_ascending`.
2. Met à jour les entrées existantes pour s'assurer qu'elles utilisent la valeur
   par défaut si le champ est NULL ou vide.
"""

from app import app, db
from sqlalchemy import text


def migrate():
    with app.app_context():
        print("[MIGRATION] Début de la migration question_order_mode...")

        try:
            print("[ETAPE 1] Ajout de la colonne 'question_order_mode' à 'quiz_rule_sets'...")
            try:
                db.session.execute(
                    text(
                        """
                        ALTER TABLE quiz_rule_sets
                        ADD COLUMN question_order_mode VARCHAR(30) NOT NULL DEFAULT 'difficulty_ascending'
                        """
                    )
                )
                db.session.commit()
                print("[OK] Colonne 'question_order_mode' ajoutée avec succès")
            except Exception as e:
                db.session.rollback()
                message = str(e).lower()
                if "duplicate column name" in message or "already exists" in message:
                    print("[INFO] La colonne 'question_order_mode' existe déjà")
                else:
                    raise

            print("[ETAPE 2] Normalisation des valeurs existantes...")
            try:
                db.session.execute(
                    text(
                        """
                        UPDATE quiz_rule_sets
                        SET question_order_mode = 'difficulty_ascending'
                        WHERE question_order_mode IS NULL OR TRIM(question_order_mode) = ''
                        """
                    )
                )
                db.session.commit()
                print("[OK] Valeurs existantes normalisées")
            except Exception as e:
                db.session.rollback()
                print("[AVERTISSEMENT] Impossible de normaliser les valeurs existantes:", e)

            print("[SUCCES] Migration terminée avec succès !")
        except Exception as e:
            print(f"[ERREUR] Erreur lors de la migration: {e}")
            db.session.rollback()
            raise


if __name__ == '__main__':
    migrate()

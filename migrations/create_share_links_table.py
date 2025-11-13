"""
Migration pour créer la table quiz_share_links
Système de liens de partage personnalisés avec UUID

Usage:
    python migrations/create_share_links_table.py
"""

import sys
import os

# Ajouter le dossier parent au path pour importer les modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from models import QuizShareLink

def create_share_links_table():
    """Crée la table quiz_share_links si elle n'existe pas"""
    with app.app_context():
        try:
            # Créer toutes les tables (celle-ci incluse)
            db.create_all()
            
            print("✅ Migration réussie !")
            print("📊 Table 'quiz_share_links' créée (ou déjà existante)")
            
            # Vérifier que la table existe
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            
            if 'quiz_share_links' in tables:
                print("✓ Table 'quiz_share_links' confirmée dans la base de données")
                
                # Afficher les colonnes
                columns = inspector.get_columns('quiz_share_links')
                print(f"\n📝 Colonnes de la table ({len(columns)}) :")
                for col in columns:
                    print(f"  - {col['name']}: {col['type']}")
            else:
                print("❌ Erreur : La table n'a pas été créée")
                return False
                
            return True
            
        except Exception as e:
            print(f"❌ Erreur lors de la migration : {str(e)}")
            import traceback
            traceback.print_exc()
            return False

if __name__ == '__main__':
    print("🚀 Démarrage de la migration...")
    print("-" * 50)
    success = create_share_links_table()
    print("-" * 50)
    
    if success:
        print("\n✅ Migration terminée avec succès !")
        print("\n💡 Prochaines étapes :")
        print("  1. Tester la création d'un lien de partage")
        print("  2. Vérifier l'affichage de la page /share/<uuid>")
        print("  3. Tester le partage sur Facebook/Twitter")
    else:
        print("\n❌ La migration a échoué.")
        print("Vérifiez les erreurs ci-dessus.")
        sys.exit(1)

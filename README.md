# TNote v4

Bloc-notes sticky minimaliste pour Windows avec synchronisation Firebase.

## Fonctionnalites

- Notes multiples (navigation Ctrl+Molette)
- Formatage riche : **gras**, *italique*, souligné, taille de police
- Toolbar flottante au survol de la selection
- 11 couleurs (blanc + 10 Bristol) avec color picker
- Raccourci global configurable (Ctrl+Alt+N par defaut)
- Redimensionnement libre sur tous les bords
- Synchronisation cloud via Firebase (Realtime Database + Auth)
- Sauvegarde automatique avec debounce
- System tray avec menu complet

## Installation

1. Installer Python 3.10+
2. Lancer `install.bat` (installe les dependances + ajoute au demarrage Windows)

Ou manuellement :
```
pip install pystray Pillow
python tnote.py
```

## Configuration Cloud (optionnel)

1. Creer un projet sur [Firebase Console](https://console.firebase.google.com)
2. Activer **Authentication** > Email/Password
3. Creer une **Realtime Database** avec ces regles :
```json
{
  "rules": {
    "users": {
      "$uid": {
        ".read": "$uid === auth.uid",
        ".write": "$uid === auth.uid"
      }
    }
  }
}
```
4. Dans TNote : clic droit tray > Parametres > renseigner API Key + Database URL + creer un compte

## Raccourcis clavier

| Raccourci | Action |
|-----------|--------|
| Ctrl+Alt+N | Afficher / Masquer |
| Ctrl+Molette | Changer de note |
| Ctrl+B | Gras |
| Ctrl+I | Italique |
| Ctrl+U | Souligne |

## Fichiers

| Fichier | Description |
|---------|-------------|
| `tnote.py` | Application principale |
| `install.bat` | Installation + ajout au demarrage |
| `lancer.bat` | Lancement rapide |

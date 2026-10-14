# Récupérateur Android

Logiciel Windows (lecture seule) pour analyser un téléphone Android branché en USB et récupérer des fichiers vers le PC : corbeille, miniatures, médias de messagerie, fichiers cachés / temporaires, photos et vidéos du stockage.

## Important

- **Aucune écriture** sur le téléphone (uniquement `adb devices`, `getprop`, `find`/`ls`/`stat`, `adb pull`).
- Sous Android 11+, certains dossiers (`Android/data`, …) peuvent être inaccessibles : l’outil journalise « accès refusé » et continue.

## Utilisation

1. Activez les options développeur et le débogage USB.
2. Lancez l’application, cliquez sur **Détecter**, choisissez l’appareil.
3. **Analyser (scan approfondi)** puis filtrez / sélectionnez les fichiers.
4. **Récupérer la sélection** vers un dossier sur le PC (rapport `.txt` inclus).

## Build (.exe)

Workflow GitHub Actions `.github/workflows/build.yml` :

1. Poussez le projet sur `main`.
2. Onglet **Actions** → téléchargez l’artifact **RecuperateurAndroid**.

## Dépendances (source)

- Python 3.11+, Tkinter
- `platform-tools` (adb) à côté de `recup_gui.py`
- Pillow optionnel (aperçu des images)

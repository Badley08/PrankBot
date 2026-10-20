# Telegram Selfie Bot (consent-based)

Bot Telegram + site Flask pour générer des liens uniques de selfie. La page explique que la photo sera envoyée au propriétaire, demande l'autorisation caméra, affiche un aperçu et nécessite une confirmation explicite avant l'envoi.

## Fonctionnalités
- `/start`, `/help`, `/link`, `/cancel`, `/stats`
- Liens à usage unique et expirant après `LINK_TTL_MINUTES`
- Webhook Telegram protégé par `TELEGRAM_WEBHOOK_SECRET`
- Page web HTTPS-compatible avec autorisation caméra explicite
- Aperçu du selfie avant envoi
- Validation du type et de la taille du fichier
- SQLite pour les liens et événements

## Variables d'environnement Render
Configure-les dans **Dashboard → ton service → Environment** :
- `TELEGRAM_BOT_TOKEN`: le nouveau token obtenu via BotFather. Ne le partage jamais.
- `TELEGRAM_BOT_USERNAME`: nom du bot sans `@`.
- `OWNER_CHAT_ID`: ton identifiant de chat Telegram numérique.
- `PUBLIC_BASE_URL`: URL publique Render, ex. `https://nom-du-service.onrender.com` (sans `/` final).
- `TELEGRAM_WEBHOOK_SECRET`: secret aléatoire de 32+ caractères composé de lettres, chiffres, `_` ou `-`.
- `OWNER_ADMIN_KEY`: secret aléatoire long pour protéger les routes d'administration.
- `DATABASE_PATH`: par défaut `/tmp/prankbot.sqlite3`.
- `LINK_TTL_MINUTES`: durée du lien, par défaut 60.
- `MAX_PHOTO_BYTES`: taille maximale, par défaut 8 MiB.

**Important :** le disque du service Render gratuit est éphémère. `/tmp` et SQLite peuvent être réinitialisés lors d'un redéploiement/redémarrage. Pour conserver les données durablement, configure un disque persistant ou utilise une base PostgreSQL managée et adapte le code.

## Déploiement
1. Crée un dépôt GitHub privé et ajoute ces fichiers.
2. Sur Render, crée un **Web Service** à partir du dépôt.
3. Vérifie `Build Command`: `pip install -r requirements.txt`.
4. Vérifie `Start Command`: `gunicorn app:app --workers 1 --threads 4 --timeout 60`.
5. Ajoute les variables d'environnement ci-dessus.
6. Déploie et copie l'URL HTTPS du service dans `PUBLIC_BASE_URL`, puis redéploie.
7. Récupère ton identifiant numérique Telegram avec un bot d'identification fiable ou via un petit script temporaire qui affiche `message.chat.id`; ne partage pas cet identifiant publiquement.
8. Une fois le service en ligne, configure le webhook avec une requête POST :

```bash
curl -X POST "https://TON-SERVICE.onrender.com/admin/set-webhook" \
  -H "X-Owner-Key: TON_OWNER_ADMIN_KEY"
```

La réponse doit contenir `"ok": true`. Pour désactiver le webhook :

```bash
curl -X POST "https://TON-SERVICE.onrender.com/admin/delete-webhook" \
  -H "X-Owner-Key: TON_OWNER_ADMIN_KEY"
```

## Utilisation
- Envoie `/start` au bot.
- Depuis le compte Telegram correspondant à `OWNER_CHAT_ID`, envoie `/link`.
- Partage le lien uniquement avec une personne qui comprend ce qu'il fait.
- Elle pourra choisir d'autoriser la caméra, prendre un selfie, voir l'aperçu et confirmer son envoi.

## Sécurité et limites
- Le token BotFather doit rester dans les variables d'environnement. Si tu l'as partagé, révoque-le et remplace-le.
- Utilise uniquement les selfies envoyés volontairement.
- Ce prototype ne stocke pas les fichiers photo sur le serveur, mais Telegram conserve le message envoyé au propriétaire selon le fonctionnement du service. Supprime le message si tu n'en as plus besoin.
- Les événements enregistrent des références et métadonnées, pas les images. Évite de journaliser des données personnelles.
- La limitation de débit en mémoire/SQLite est basique, pas un système anti-abus complet.
- Le stockage SQLite sur `/tmp` n'est pas durable sur Render gratuit.
- Le lien à usage unique est consommé avant l'envoi à Telegram afin de limiter les doubles envois; en cas de panne Telegram, il faudra générer un nouveau lien.
- Pour une utilisation réelle à plus grande échelle, ajoute PostgreSQL, une politique de confidentialité, un mécanisme de suppression et une surveillance des erreurs.

#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
name="backup-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite3"
./scripts/production.sh exec -T bot python -c 'import sqlite3,sys; src=sqlite3.connect("/app/data/bot.sqlite3"); dst=sqlite3.connect("/app/data/"+sys.argv[1]); src.backup(dst); result=dst.execute("PRAGMA integrity_check").fetchone()[0]; dst.close(); src.close(); assert result=="ok"' "$name"
./scripts/production.sh cp "bot:/app/data/$name" "backups/$name"
./scripts/production.sh exec -T bot python -c 'import os,sys; os.unlink("/app/data/"+sys.argv[1])' "$name"
echo "Backup saved: backups/$name"

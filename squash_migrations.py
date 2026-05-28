"""Squash Plane's Django migrations for faster fresh installs.

Run at container boot before supervisor starts. Patches MigrationLoader
to skip the DB connection (squashmigrations only reads migration files).
Replaces broken RunPython references with no-ops since data migrations
are no-ops on an empty database anyway.
"""
import os
import re
import sys

BACKEND_DIR = os.environ.get("BACKEND_DIR", "/app/backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "plane.settings.production")
os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost/x")
os.environ.setdefault("SECRET_KEY", "build-only")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from django.db.migrations.loader import MigrationLoader

_orig_init = MigrationLoader.__init__

def _no_db_init(self, connection, *args, **kwargs):
    _orig_init(self, None, *args, **kwargs)

MigrationLoader.__init__ = _no_db_init

import django
django.setup()

from django.core.management import call_command


def fix_squashed_migration(path):
    """Replace broken RunPython function references with no-ops."""
    with open(path) as f:
        content = f.read()

    original = content
    content = re.sub(
        r'code=plane\.\w+\.migrations\.\d[\w.]*',
        'code=migrations.RunPython.noop',
        content,
    )

    if content != original:
        with open(path, 'w') as f:
            f.write(content)
        print(f"  Fixed RunPython references in {os.path.basename(path)}")


def squash_app(app_label, last_migration, migrations_dir):
    migrations_dir = os.path.join(BACKEND_DIR, migrations_dir)
    squashed = os.path.join(migrations_dir, "0001_squashed.py")
    if os.path.exists(squashed):
        return

    print(f"  Squashing {app_label} migrations...")
    call_command(
        "squashmigrations", app_label, last_migration,
        squashed_name="squashed", interactive=False,
    )

    if os.path.exists(squashed):
        fix_squashed_migration(squashed)


print("Squashing migrations for faster first boot...")
squash_app("db", "0097", "plane/db/migrations")
squash_app("license", "0005", "plane/license/migrations")
print("Done.")

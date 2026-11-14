#!/usr/bin/env python3
"""
Database initialization script for Solar Acquisition Platform.

Usage:
    python scripts/init_db.py [--drop]

Options:
    --drop    Drop existing database before creating (WARNING: destructive)
"""

import os
import sys
import argparse
from pathlib import Path

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    print("Error: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("Warning: python-dotenv not installed. Using environment variables only.")
    load_dotenv = None

# Load environment variables
if load_dotenv:
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)

# Configuration
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'solar_acquisition')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

# Or use DATABASE_URL if provided
DATABASE_URL = os.getenv('DATABASE_URL')


def parse_database_url(url: str) -> dict:
    """Parse PostgreSQL connection URL into components."""
    # Format: postgresql://user:password@host:port/dbname
    from urllib.parse import unquote, urlparse
    parsed = urlparse(url)
    return {
        'host': parsed.hostname or 'localhost',
        'port': parsed.port or 5432,
        'dbname': unquote(parsed.path.lstrip('/')) or 'solar_acquisition',
        'user': unquote(parsed.username or 'postgres'),
        'password': unquote(parsed.password or ''),
    }


def get_connection_params() -> dict:
    """Get database connection parameters."""
    if DATABASE_URL:
        return parse_database_url(DATABASE_URL)
    return {
        'host': DB_HOST,
        'port': int(DB_PORT),
        'dbname': DB_NAME,
        'user': DB_USER,
        'password': DB_PASSWORD,
    }


def create_database(params: dict, drop_existing: bool = False):
    """Create the database if it doesn't exist."""
    dbname = params['dbname']

    # Connect to default 'postgres' database to create our database
    conn_params = params.copy()
    conn_params['dbname'] = 'postgres'

    print(f"Connecting to PostgreSQL at {params['host']}:{params['port']}...")

    try:
        conn = psycopg2.connect(**conn_params)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()

        # Check if database exists
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (dbname,)
        )
        exists = cur.fetchone()

        if exists:
            if drop_existing:
                print(f"Dropping existing database '{dbname}'...")
                # Terminate existing connections
                cur.execute("""
                    SELECT pg_terminate_backend(pg_stat_activity.pid)
                    FROM pg_stat_activity
                    WHERE pg_stat_activity.datname = %s
                    AND pid <> pg_backend_pid()
                """, (dbname,))
                cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
                print(f"Database '{dbname}' dropped.")
            else:
                print(f"Database '{dbname}' already exists.")
                cur.close()
                conn.close()
                return True

        # Create database
        print(f"Creating database '{dbname}'...")
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        print(f"Database '{dbname}' created successfully.")

        cur.close()
        conn.close()
        return True

    except psycopg2.OperationalError as e:
        print(f"Error: Could not connect to PostgreSQL: {e}")
        print("\nMake sure PostgreSQL is running and the credentials are correct.")
        print("Check your .env file or environment variables:")
        print(f"  DB_HOST={DB_HOST}")
        print(f"  DB_PORT={DB_PORT}")
        print(f"  DB_USER={DB_USER}")
        return False


def run_schema(params: dict):
    """Run the schema.sql file to create tables."""
    schema_path = Path(__file__).parent.parent / 'schema.sql'

    if not schema_path.exists():
        print(f"Error: Schema file not found at {schema_path}")
        return False

    print(f"Reading schema from {schema_path}...")
    with open(schema_path, 'r') as f:
        schema_sql = f.read()

    print(f"Connecting to database '{params['dbname']}'...")

    try:
        conn = psycopg2.connect(**params)
        cur = conn.cursor()

        print("Running schema migrations...")
        cur.execute(schema_sql)
        conn.commit()

        # Verify tables were created
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = cur.fetchall()

        print(f"\nCreated {len(tables)} tables:")
        for (table,) in tables:
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
            count = cur.fetchone()[0]
            print(f"  - {table} ({count} rows)")

        # Check views
        cur.execute("""
            SELECT table_name
            FROM information_schema.views
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        views = cur.fetchall()

        if views:
            print(f"\nCreated {len(views)} views:")
            for (view,) in views:
                print(f"  - {view}")

        cur.close()
        conn.close()

        print("\nSchema migration completed successfully!")
        return True

    except psycopg2.Error as e:
        print(f"Error running schema: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Initialize Solar Acquisition database')
    parser.add_argument('--drop', action='store_true',
                       help='Drop existing database before creating')
    args = parser.parse_args()

    if args.drop:
        print("WARNING: This will delete all existing data!")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            sys.exit(0)

    params = get_connection_params()

    print("=" * 60)
    print("Solar Acquisition Platform - Database Initialization")
    print("=" * 60)
    print()

    # Step 1: Create database
    if not create_database(params, drop_existing=args.drop):
        sys.exit(1)

    print()

    # Step 2: Run schema
    if not run_schema(params):
        sys.exit(1)

    print()
    print("=" * 60)
    print("Database initialization complete!")
    print("=" * 60)


if __name__ == '__main__':
    main()

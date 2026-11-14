"""
Database connection and session management for Solar Acquisition Platform.
"""

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database URL
DATABASE_URL = os.getenv(
    'DATABASE_URL',
    'postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/solar_acquisition'
)

# Keep existing unqualified PostgreSQL URLs on the installed driver.
database_url = make_url(DATABASE_URL)
if database_url.drivername == "postgresql":
    database_url = database_url.set(drivername="postgresql+psycopg2")

engine = create_engine(
    database_url,
    echo=os.getenv('DB_ECHO', 'false').lower() == 'true',
    pool_pre_ping=True,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Get a database session with automatic cleanup."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_connection() -> bool:
    """Test database connection."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False


def get_table_counts() -> dict:
    """Get row counts for all tables."""
    tables = ['targets', 'cadastral_records', 'owners', 'scores', 'outreach_log', 'memos']
    counts = {}

    with engine.connect() as conn:
        for table in tables:
            try:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                counts[table] = result.scalar()
            except Exception:
                counts[table] = None

    return counts


if __name__ == '__main__':
    print("Testing database connection...")
    if test_connection():
        print("Connection successful!")
        print("\nTable counts:")
        for table, count in get_table_counts().items():
            print(f"  {table}: {count}")
    else:
        print("Connection failed!")

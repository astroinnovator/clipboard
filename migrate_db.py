#!/usr/bin/env python3
"""
Database Migration Script - Add secret_key column to users table
This script safely adds the secret_key column to existing databases.
"""

import os
import sys
from sqlalchemy import create_engine, text, inspect
from dotenv import load_dotenv


def main():
    print("🔄 Starting database migration...")

    # Load environment variables
    load_dotenv()

    # Get database URL
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL environment variable not set")
        sys.exit(1)

    # Convert postgresql:// to postgresql+psycopg:// for SQLAlchemy compatibility
    database_url = DATABASE_URL
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    try:
        # Create database engine
        print(f"📡 Connecting to database...")
        engine = create_engine(database_url)

        # Check if database is accessible
        with engine.connect() as conn:
            print("✅ Database connection successful")

            # Check if users table exists
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            if "users" not in tables:
                print(
                    "❌ ERROR: users table does not exist. Please run the main application first to create tables."
                )
                sys.exit(1)

            print("✅ Users table found")

            # Check if secret_key column already exists
            columns = inspector.get_columns("users")
            column_names = [col["name"] for col in columns]

            if "secret_key" in column_names:
                print("✅ secret_key column already exists. No migration needed.")
                return

            print("🔧 Adding secret_key column to users table...")

            # Add the secret_key column
            migration_sql = """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS secret_key VARCHAR(100);
            """

            conn.execute(text(migration_sql))
            conn.commit()

            print("✅ Successfully added secret_key column")

            # Verify the column was added
            inspector = inspect(engine)
            columns = inspector.get_columns("users")
            column_names = [col["name"] for col in columns]

            if "secret_key" in column_names:
                print("✅ Migration verified: secret_key column exists")
                print("\n📋 Current users table schema:")
                for col in columns:
                    nullable = "NULL" if col["nullable"] else "NOT NULL"
                    print(f"  - {col['name']}: {col['type']} ({nullable})")
            else:
                print(
                    "❌ ERROR: Migration failed - secret_key column not found after creation"
                )
                sys.exit(1)

    except Exception as e:
        print(f"❌ Migration failed: {e}")
        sys.exit(1)

    print("\n🎉 Database migration completed successfully!")
    print("💡 You can now deploy your application with the updated schema.")


if __name__ == "__main__":
    main()

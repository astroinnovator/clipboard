"""
Vercel serverless function for database migration
Adds secret_key column to users table if it doesn't exist
"""

import os
import json
from sqlalchemy import create_engine, text, inspect
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/api/migrate")
async def migrate_database():
    """
    Migrate database to add secret_key column to users table
    """
    try:
        # Get database URL from environment
        DATABASE_URL = os.getenv("DATABASE_URL")
        if not DATABASE_URL:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "DATABASE_URL environment variable not set",
                },
            )

        # Convert postgresql:// to postgresql+psycopg:// for SQLAlchemy compatibility
        database_url = DATABASE_URL
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )

        # Create database engine
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # Check if users table exists
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            if "users" not in tables:
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "users table does not exist. Run main app first.",
                    },
                )

            # Check if secret_key column already exists
            columns = inspector.get_columns("users")
            column_names = [col["name"] for col in columns]

            if "secret_key" in column_names:
                return JSONResponse(
                    content={
                        "success": True,
                        "message": "secret_key column already exists. No migration needed.",
                        "schema": [
                            {
                                "name": col["name"],
                                "type": str(col["type"]),
                                "nullable": col["nullable"],
                            }
                            for col in columns
                        ],
                    }
                )

            # Add the secret_key column
            migration_sql = """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS secret_key VARCHAR(100);
            """

            conn.execute(text(migration_sql))
            conn.commit()

            # Verify the column was added
            inspector = inspect(engine)
            columns = inspector.get_columns("users")
            column_names = [col["name"] for col in columns]

            if "secret_key" in column_names:
                return JSONResponse(
                    content={
                        "success": True,
                        "message": "Successfully added secret_key column",
                        "schema": [
                            {
                                "name": col["name"],
                                "type": str(col["type"]),
                                "nullable": col["nullable"],
                            }
                            for col in columns
                        ],
                    }
                )
            else:
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Migration failed - column not found after creation",
                    },
                )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": f"Migration failed: {str(e)}"},
        )


# For Vercel compatibility
def handler(request):
    """Vercel handler function"""
    return app(request)

#!/bin/bash

#fix

set -e

echo "🚀 Starting Clipboard Management System on Render"
echo "================================================="

# Set default port if not provided by Render
export PORT=${PORT:-8000}

# Apply the server fix for form handling
echo "🔧 Applying server patches..."
python3 -c "
import re
from pathlib import Path

server_file = Path('server.py')
content = server_file.read_text()

# Fix form.get().strip() issues to prevent NoneType errors
fixes = [
    (r'form\.get\(\"username\"\)\.strip\(\)', 'form.get(\"username\", \"\").strip()'),
    (r'form\.get\(\"password\"\)\.strip\(\)', 'form.get(\"password\", \"\").strip()'),
    (r'form\.get\(\"secret_key\"\)\.strip\(\)', 'form.get(\"secret_key\", \"\").strip()'),
    (r'form\.get\(\"role\"\)\.strip\(\)', 'form.get(\"role\", \"\").strip()'),
    (r'form\.get\(\"user_id\"\)', 'form.get(\"user_id\", \"\")'),
    (r'form\.get\(\"new_username\"\)\.strip\(\)', 'form.get(\"new_username\", \"\").strip()'),
    (r'form\.get\(\"new_password\"\)\.strip\(\)', 'form.get(\"new_password\", \"\").strip()'),
]

for pattern, replacement in fixes:
    content = re.sub(pattern, replacement, content)

server_file.write_text(content)
print('✅ Server patches applied')
"

# Verify environment variables
echo "🔍 Checking environment variables..."
if [ -z "$DATABASE_URL" ]; then
    echo "⚠️  DATABASE_URL not set - using fallback"
fi

# Print configuration (without sensitive data)
echo "📋 Configuration:"
echo "  Port: $PORT"
echo "  Environment: ${ENVIRONMENT:-development}"
echo "  Python Version: $(python3 --version)"

# Verify database connectivity (if URL is set)
if [ ! -z "$DATABASE_URL" ]; then
    echo "🗄️  Testing database connection..."
    python3 -c "
import os
try:
    from sqlalchemy import create_engine, text
    db_url = os.getenv('DATABASE_URL', '')
    if db_url.startswith('postgresql://'):
        db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    print('✅ Database connection successful')
except Exception as e:
    print(f'⚠️  Database connection warning: {e}')
    print('   App will continue - database will be initialized on first request')
" || echo "⚠️  Database test failed - continuing anyway"
fi

# Create necessary directories
echo "📁 Creating application directories..."
mkdir -p logs uploads

# Set proper permissions
chmod +x server.py 2>/dev/null || true

# Start the application
echo "🎯 Starting FastAPI application on port $PORT"
echo "=============================================="

# Use uvicorn to start the server
exec uvicorn server:app \
    --host 0.0.0.0 \
    --port $PORT \
    --workers 1 \
    --access-log \
    --log-level info

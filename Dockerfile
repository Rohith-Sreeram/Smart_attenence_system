FROM python:3.9-slim

# Install system dependencies for OpenCV and SQLite
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the port (Render uses PORT env var)
EXPOSE 8000

# Command to run the application
# We use 0.0.0.0 to make it accessible outside the container
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}

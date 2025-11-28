# Use python slim
FROM python:3.10-slim

# Set working directory to /app
WORKDIR /app

# Copy the requirements file from the root
COPY requirements.txt .

# Install dependencies
# (Explicitly ensure flask deps are here if requirements.txt missed them)
RUN pip install --no-cache-dir -r requirements.txt gunicorn flask sqlalchemy psycopg2-binary requests python-dotenv

# Copy the web app source code into /app
COPY web_app/ .

# Expose port
EXPOSE 10000

# Start Gunicorn
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:10000", "app:app"]
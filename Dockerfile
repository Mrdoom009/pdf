FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose port (adjust if your Flask app uses a different one)
EXPOSE 8000

# Run script
CMD ["./run.sh"]

FROM python:3.10-slim

# Set environment variables
ENV TZ=Europe/Berlin
ENV DEBIAN_FRONTEND=noninteractive

# Install system packages and tzdata for timezone
RUN apt-get update && \
    apt-get install -y tzdata cron && \
    ln -fs /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    dpkg-reconfigure --frontend noninteractive tzdata && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy your application code and requirements
COPY control_and_cleanup.py /app/
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy crontab file and setup
COPY crontab.txt /etc/cron.d/server-cron

# Apply cron job file & give execution rights
RUN chmod 0644 /etc/cron.d/server-cron && \
    crontab /etc/cron.d/server-cron

# Start cron and keep container alive
CMD ["cron", "-f"]

FROM python:3.12-slim
# Railway cloud deployment

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Copy application files
COPY post_call_digest.py .
COPY missing_transcripts_report.py .
COPY entrypoint.sh .
COPY reference/ reference/

# Seed databases (copied to persistent volume on first run)
COPY transcripts.db /app/seed/transcripts.db
COPY drafts.db /app/seed/drafts.db

# The script uses only stdlib, no pip install needed

CMD ["bash", "entrypoint.sh"]

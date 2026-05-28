FROM python:3.10-slim

# Hugging Face requires a non-root user
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Copy requirements and install
COPY --chown=user requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your bot files
COPY --chown=user . .

# Run the bot
CMD ["python", "main.py"]

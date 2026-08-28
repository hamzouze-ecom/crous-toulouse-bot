FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Copie des fichiers de dépendances
COPY requirements.txt .

# Installation des dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copie du reste des fichiers de l'application
COPY . .

# Lancement du bot Crous
CMD ["python", "crous_bot.py"]

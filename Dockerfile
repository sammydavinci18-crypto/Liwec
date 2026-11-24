# OPTIONAL. Use this only if you want teachers to upload .pptx files in
# production (see README, "PowerPoint files need LibreOffice"). The normal
# Python deployment works without it, but accepts PDF slides only.
# NOTE: not built or tested by the author of this change; verify the first deploy.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libreoffice-impress fonts-liberation fonts-dejavu-core fonts-noto-core fonts-crosextra-carlito fonts-crosextra-caladea \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Same command as the Procfile / render.yaml.
CMD gunicorn --worker-class gthread --threads 4 --timeout 120 -w 1 --bind 0.0.0.0:${PORT:-10000} app:app

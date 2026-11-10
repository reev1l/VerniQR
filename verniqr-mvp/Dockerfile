FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 verniqr
COPY pyproject.toml requirements.lock ./
COPY verniqr ./verniqr
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/data && chown -R verniqr:verniqr /app/data
USER verniqr
EXPOSE 8000
CMD ["python", "-m", "verniqr", "run"]

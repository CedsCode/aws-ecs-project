# Pinned to amd64 so an image built on an ARM laptop still runs on x86 Fargate/EC2.
FROM --platform=linux/amd64 python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shown in the UI and logs. Build with --build-arg APP_VERSION=$(git rev-parse --short HEAD).
# Declared after pip install so a new version doesn't bust the dependency layer.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

COPY app/ app/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8000

# Exec form: uvicorn is PID 1 and receives SIGTERM from ECS directly.
# The app writes its own JSON request logs, so uvicorn's access log is off.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

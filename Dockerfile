FROM python:3.12-slim

# The package version is derived from git tags (setuptools-scm), but .git is
# not in the build context — CI passes the released version as a build arg;
# local builds get the 0.0.0 placeholder.
ARG APP_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_NEWS_TG_BOT=$APP_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY feedspec/ feedspec/
COPY fetcher/ fetcher/
COPY archive/ archive/
COPY newsbot/ newsbot/
COPY cli.py specsource.py ./

RUN pip install --no-cache-dir .[translate]

# No live configuration is baked into the image — it is publishable as-is.
# Deployments inject the spec via SPEC_JSON (from a secret store), SPEC_URL,
# or a mounted file at SPEC_PATH. The example and schema ship for smoke
# tests (`python -m cli validate --spec spec.example.json`); schemas/ holds
# the XSDs a spec's schema_file may reference (resolved against
# SPEC_BASE_DIR, default the working directory).
COPY spec.example.json spec.schema.json ./
COPY schemas/ schemas/

# One image, three units — the fetch job (this default), the webhook bot
# (`python -m cli serve`) and the migrate job (`alembic upgrade head &&
# python -m cli register-webhook`) override the command.
CMD ["python", "-m", "cli", "run", "--once"]

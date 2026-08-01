# The application image every `webapp-postgres` environment runs.
#
# Built once, out of band, and tagged. The provisioner starts pre-existing
# images and never builds anything (CLAUDE.md §1), so this is a prerequisite,
# not a step in the provisioning path:
#
#   docker build -t provisioner/demo-app:1 templates/demo-app
#
FROM python:3.12-slim

WORKDIR /srv

# Pinned, and pinned to the same majors as the provisioner's own requirements.
# psycopg 3 rather than psycopg2: this app is new code, and the binary wheel
# needs no build toolchain in the image.
RUN pip install --no-cache-dir \
      fastapi==0.111.0 \
      "uvicorn[standard]==0.29.0" \
      "psycopg[binary]==3.1.19"

COPY app.py .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

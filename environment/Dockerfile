ARG PYTHON_IMAGE=python:3.12.9-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY environment/requirements.txt /app/environment/requirements.txt
RUN pip install --no-cache-dir -r environment/requirements.txt && useradd --uid 10001 --create-home esa
COPY environment/*.py environment/catalog.json environment/manifest.json environment/seed-v1.json environment/seed-schema.json /app/environment/
COPY configs/tools.json /app/configs/tools.json
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8003
CMD ["python", "-m", "environment.server"]

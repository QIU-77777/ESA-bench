ARG PYTHON_IMAGE=python:3.12.9-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY cockpit/recorder.py /app/cockpit/recorder.py
COPY environment/*.py /app/environment/
COPY environment/requirements.txt /app/environment/requirements.txt
RUN pip install --no-cache-dir -r environment/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8003
CMD ["python", "-m", "cockpit.recorder"]

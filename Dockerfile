FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml app.py ./
RUN pip install --no-cache-dir .
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
ENV TASK_DB=/data/tasks.sqlite3
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

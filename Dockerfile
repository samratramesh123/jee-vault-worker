FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt && \
    playwright install --with-deps chromium

COPY render_pdf_worker.py .
RUN mkdir -p /app/output

EXPOSE 8000

CMD ["uvicorn", "render_pdf_worker:app", "--host", "0.0.0.0", "--port", "8000"]

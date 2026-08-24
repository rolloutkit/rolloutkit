FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0"

WORKDIR /app
COPY app.py .

EXPOSE 8000
# exec-form on purpose: shell-form would put /bin/sh at PID 1 and the signal
# might never reach uvicorn.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

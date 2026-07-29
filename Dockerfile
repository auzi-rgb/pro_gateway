FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx jinja2 python-multipart passlib[bcrypt] python-jose[cryptography] bcrypt==4.0.1

COPY . /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

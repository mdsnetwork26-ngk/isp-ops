FROM python:3.11-slim
WORKDIR /opt/isp-ops
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY templates ./templates
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]

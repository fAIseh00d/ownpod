FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY handler.py .
EXPOSE 8000
CMD ["python","handler.py","--rp_serve_api","--rp_api_host","0.0.0.0","--rp_api_port","8000"]

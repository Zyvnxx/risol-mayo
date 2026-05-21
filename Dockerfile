FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Override at deploy time:
#   docker run -p 7860:7860 \
#     -e ADMIN_PASSWORD=secret \
#     -v $(pwd)/config.json:/app/config.json:ro \
#     admin-panel
ENV ADMIN_HOST=0.0.0.0 \
    ADMIN_PORT=7860

EXPOSE 7860

CMD ["python", "index.py"]

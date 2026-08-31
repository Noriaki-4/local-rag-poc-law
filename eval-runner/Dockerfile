FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run_eval.py .
COPY lawqa_jp ./lawqa_jp

CMD ["python", "run_eval.py"]

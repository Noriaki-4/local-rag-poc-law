FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /runtime

COPY agent-api/requirements.txt ./requirements.txt
COPY infra/aws/requirements.txt ./aws-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r aws-requirements.txt

COPY agent-api/app ./app
COPY infra/aws/agentcore/contract.py ./contract.py
COPY infra/aws/agentcore/runtime_app.py ./runtime_app.py
COPY infra/aws/agentcore/aws_adapters.py ./aws_adapters.py

EXPOSE 8080

CMD ["uvicorn", "runtime_app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

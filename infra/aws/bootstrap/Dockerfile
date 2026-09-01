FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /bootstrap

COPY infra/aws/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY infra/aws/scripts/bootstrap_aws_data.py ./bootstrap_aws_data.py
COPY infra/aws/bootstrap/bootstrap_entrypoint.py ./bootstrap_entrypoint.py

CMD ["python", "bootstrap_entrypoint.py"]

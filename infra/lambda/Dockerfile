# Part 1 detector as a Lambda container image.
# Build context is the repo root: docker build -f infra/lambda/Dockerfile .
FROM public.ecr.aws/lambda/python:3.12-arm64@sha256:26002382ad26d29f82a39e536408da7af431dc0de76a08f858ffccbc9743cb8c

# Dependencies first, for layer caching.
COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-runtime.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

COPY config ./config
COPY infra/lambda/handler.py ${LAMBDA_TASK_ROOT}/handler.py
COPY infra/lambda/smoke.py ${LAMBDA_TASK_ROOT}/smoke.py
COPY infra/lambda/Dockerfile ${LAMBDA_TASK_ROOT}/Dockerfile

ENV LOAN_DQ_CONFIG=/var/task/config/default.yaml

CMD ["handler.on_event"]

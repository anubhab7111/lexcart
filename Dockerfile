# Evaluation image: LexCart's agentic-commerce surface in lite mode.
# One `docker compose up` gives an evaluator the full UI + API + mock
# Razorpay gateway with zero local dependencies. The LLM/RAG legal-platform
# features need the full local setup (see README) and are not in this image.

FROM node:20-slim AS client-build
WORKDIR /client
COPY client/package.json client/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY client/ .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app/server
COPY server/requirements-lite.txt .
RUN pip install --no-cache-dir -r requirements-lite.txt
COPY server/ .
COPY demo/ /app/demo/
COPY --from=client-build /client/build ./frontend
ENV LEXCART_LITE=1
EXPOSE 8000
CMD ["sh", "-c", "python -m app.db.init_db && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]

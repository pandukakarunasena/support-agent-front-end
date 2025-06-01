# # ─── Stage 1: Builder ───────────────────────────────────────────────
# FROM python:3.10-slim AS builder

# WORKDIR /app

# # Install Python dependencies into user site
# RUN apt-get update && apt-get install -y python3 python3-pip
# COPY requirements.txt ./
# RUN pip install --no-cache-dir --user -r requirements.txt

# # Copy application source
# COPY . ./

# # ─── Stage 2: Runtime ───────────────────────────────────────────────
# FROM python:3.10-slim

# WORKDIR /app

# # Create a non-root user and group
# RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

# # Copy Python packages installed in builder
# COPY --from=builder /root/.local /home/appuser/.local

# # Copy application code
# COPY --from=builder /app /app

# # Create logs directory and set permissions
# RUN mkdir -p logs && chown -R appuser:appuser logs
# VOLUME ["/app/logs"]

# # Ensure user site packages are on PATH
# ENV PATH=/home/appuser/.local/bin:$PATH

# # Declare critical env vars (to be provided at runtime)
# # ENV OPENAI_API_KEY=""
# # ENV WSO2_TOKEN_URL=""
# # ENV WSO2_CLIENT_ID=""
# # ENV WSO2_CLIENT_SECRET=""
# ENV WSO2_UPDATE_API="https://apis.wso2.com/ocwn/updates-server/updates-803/v1.0/updates/product-update-levels"

# # Switch to non-root user
# USER appuser

# # Expose application port
# EXPOSE 8000

# # Default command to launch the API server
# CMD ["python3", "mcp_api.py"]

FROM python:alpine

WORKDIR /python-docker

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .
RUN ls
# Create a new user with UID 10016
RUN addgroup -g 10016 choreo && \
    adduser  --disabled-password  --no-create-home --uid 10016 --ingroup choreo choreouser
USER 10016
EXPOSE 5000
CMD [ "flask", "run", "--host=0.0.0.0"]

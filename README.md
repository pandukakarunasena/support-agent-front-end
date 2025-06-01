docker run -d \
  --env-file .env \
  -p 9999:9999 \
  u2-git-mcp-server:latest

docker network create mynet

docker run --rm -it \
  --name mcp_client \
  --network mynet \
  --env-file .env.run \
  -p 8000:8000 \
  -v "$(pwd)/logs":/app/logs \
    support-agent:latest



docker run --rm -it \
  --name mcp_client \
  --env-file .env.run \
  -p 8000:8000 \
  -v "$(pwd)/logs":/app/logs \



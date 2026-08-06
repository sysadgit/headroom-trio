# To install dependencies
uv sync --all-extras

# Run locally
uv run headroom proxy --port 8787

# Build as container
docker build -t headroom-trio .

# show containers running
docker ps

# stop running container
docker stop old_container_id

# remove old running container
docker rm old_container_id

# run container
docker run -d --name headroom-trio -p 8787:8787 --env-file .env -v headroom_workspace:/home/nonroot/.headroom headroom-trio

# save docker image as tar file
docker save -o headroom-trio.tar headroom-trio:latest

# send the tar file to server

# load the tar file into docker images on server
sudo docker load -i headroom-trio.tar

# stop running container
docker stop old_container_id

# remove old running container
docker rm old_container_id

# run container on server
sudo docker run -d --name headroom-trio -p 8787:8787 --env-file .env -v headroom_workspace:/home/nonroot/.headroom headroom-trio

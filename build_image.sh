#!/bin/bash
# Build and push multi-arch Docker image
IMAGE_NAME="killermatt/fan-controller"
TAG="v3"

echo "Building multi-arch image: $IMAGE_NAME:$TAG for linux/amd64 and linux/arm64"

# Check if buildx builder exists, if not create one
if ! docker buildx inspect mybuilder > /dev/null 2>&1; then
    docker buildx create --name mybuilder --use
fi

# Ensure the builder is bootstrapped
docker buildx inspect --bootstrap

# Build and push
docker buildx build --platform linux/amd64,linux/arm64 -t "$IMAGE_NAME:$TAG" --push .

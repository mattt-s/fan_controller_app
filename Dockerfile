# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies that might be needed for pyserial or other libs
# (Debian/Ubuntu base) - Add others if necessary
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install debugpy
# Copy the rest of the application code into the container at /app
# Copy only necessary files
COPY requirements.txt .
COPY app.py .
COPY static static
COPY templates templates
# Config directory is optional to copy if you want defaults baked in, 
# but usually it's mounted. create mount point.


# Make port 4812 available to the world outside this container
EXPOSE 4812

# Define environment variables if needed (e.g., for configuration)
# ENV SERIAL_PORT=/dev/ttyUSB0 # Can be overridden at runtime

# Command to run the application when the container launches
# Use gunicorn for a more robust production server (optional but recommended)
# RUN pip install --no-cache-dir gunicorn
# CMD ["gunicorn", "--bind", "0.0.0.0:4812", "app:app"]

# Or just run with Flask's built-in server (less performant, okay for simple use)
CMD ["python", "app.py"]
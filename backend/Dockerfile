FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime
WORKDIR /backend
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /backend/data /backend/uploads/images /backend/uploads/false_positives
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && chown -R app /srv
USER app
EXPOSE 8000
# one worker: the KB index and rate-limiter state are in-process
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "wsgi:app"]

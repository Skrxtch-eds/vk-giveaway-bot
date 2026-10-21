FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home appuser && mkdir /app/data && chown -R appuser:appuser /app
COPY --chown=appuser:appuser giveaway /app/giveaway
USER appuser
CMD ["python", "-m", "giveaway", "run"]
